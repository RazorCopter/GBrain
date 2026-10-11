from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from gbrain.domain.connectors import (
    BudgetExhaustedError,
    ConnectorError,
    ConnectorErrorCategory,
    RequestBudget,
)
from gbrain.domain.gmail import GmailHistoryCursor, GmailLocator, GmailSelection
from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway, RetryPolicy


class _Request:
    def __init__(self, action: Callable[[], object]) -> None:
        self.action = action

    def execute(self, *, num_retries: int = 0) -> object:
        assert num_retries == 0
        return self.action()


class _Resource:
    def __init__(self) -> None:
        self.responses: dict[str, list[object]] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def queue(self, operation: str, *responses: object) -> None:
        self.responses.setdefault(operation, []).extend(responses)

    def _request(self, operation: str, kwargs: dict[str, object]) -> _Request:
        self.calls.append((operation, kwargs))

        def action() -> object:
            queued = self.responses.get(operation, [])
            if not queued:
                raise AssertionError(f"Risposta non configurata per {operation}")
            value = queued.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        return _Request(action)

    def getProfile(self, **kwargs: object) -> _Request:
        return self._request("profile", kwargs)

    def messages(self) -> _Resource:
        return self

    def history(self) -> _Resource:
        return self

    def list(self, **kwargs: object) -> _Request:
        operation = "history" if "startHistoryId" in kwargs else "list"
        return self._request(operation, kwargs)

    def get(self, **kwargs: object) -> _Request:
        return self._request("get", kwargs)


class _Service:
    def __init__(self, resource: _Resource) -> None:
        self.resource = resource

    def users(self) -> _Resource:
        return self.resource


def _http_error(status: int, retry_after: str | None = None) -> HttpError:
    headers = {"retry-after": retry_after} if retry_after else {}
    response = type("Response", (), {"status": status, "reason": "fixture", "headers": headers})()
    return HttpError(response, b'{"error":{"message":"fixture"}}')


def _message(*, labels: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": "message-1",
        "threadId": "thread-1",
        "historyId": "50",
        "labelIds": labels or ["SENT"],
        "internalDate": "1704103200000",
        "sizeEstimate": 10,
        "payload": {"mimeType": "text/plain", "headers": [], "body": {"data": "dGVzdA"}},
    }


def test_official_gateway_hardcodes_sent_and_full_format() -> None:
    resource = _Resource()
    resource.queue("list", {"messages": [{"id": "message-1", "threadId": "thread-1"}]})
    resource.queue("get", _message())
    gateway = OfficialGmailGateway(_Service(resource))  # type: ignore[arg-type]
    selection = GmailSelection(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC), 10
    )

    page = gateway.list_sent_page(selection, None, RequestBudget(5, 10))
    gateway.fetch_sent_message(page.items[0], RequestBudget(5, 10))

    list_args = resource.calls[0][1]
    get_args = resource.calls[1][1]
    assert list_args["labelIds"] == ["SENT"]
    assert list_args["includeSpamTrash"] is False
    assert str(list_args["q"]).startswith("after:")
    assert get_args == {"userId": "me", "id": "message-1", "format": "full"}


def test_official_gateway_rejects_non_sent_direct_fetch() -> None:
    resource = _Resource()
    resource.queue("get", _message(labels=["INBOX"]))
    gateway = OfficialGmailGateway(_Service(resource))  # type: ignore[arg-type]

    with pytest.raises(ConnectorError) as captured:
        gateway.fetch_sent_message(GmailLocator("message-1"), RequestBudget(2, 2))

    assert captured.value.category == ConnectorErrorCategory.PERIMETER


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry_is_bounded_for_quota_and_transient_statuses(status: int) -> None:
    resource = _Resource()
    resource.queue("profile", _http_error(status), _http_error(status), {"emailAddress": "x"})
    delays: list[float] = []
    gateway = OfficialGmailGateway(
        _Service(resource),  # type: ignore[arg-type]
        retry_policy=RetryPolicy(max_attempts=3, initial_delay_seconds=1, jitter_ratio=0),
        sleep=delays.append,
    )
    budget = RequestBudget(3, 2)

    assert gateway.get_profile(budget)["emailAddress"] == "x"
    assert budget.requests_used == 3
    assert delays == [1.0, 2.0]


def test_budget_stops_retry_before_an_extra_request() -> None:
    resource = _Resource()
    resource.queue("profile", _http_error(500), {"emailAddress": "never"})
    gateway = OfficialGmailGateway(
        _Service(resource),  # type: ignore[arg-type]
        retry_policy=RetryPolicy(max_attempts=3),
        sleep=lambda _: None,
    )

    with pytest.raises(BudgetExhaustedError):
        gateway.get_profile(RequestBudget(1, 1))
    assert len(resource.calls) == 1


def test_auth_is_not_retried() -> None:
    resource = _Resource()
    resource.queue("profile", _http_error(401), {"emailAddress": "never"})
    gateway = OfficialGmailGateway(_Service(resource))  # type: ignore[arg-type]

    with pytest.raises(ConnectorError) as captured:
        gateway.get_profile(RequestBudget(5, 1))
    assert captured.value.category == ConnectorErrorCategory.AUTH
    assert len(resource.calls) == 1


def test_history_404_is_checkpoint_expired_and_history_args_are_sent_only() -> None:
    resource = _Resource()
    resource.queue("history", _http_error(404))
    gateway = OfficialGmailGateway(_Service(resource))  # type: ignore[arg-type]

    with pytest.raises(ConnectorError) as captured:
        gateway.list_history_page(GmailHistoryCursor("old"), RequestBudget(2, 2))

    assert captured.value.category == ConnectorErrorCategory.HISTORY_EXPIRED
    arguments = resource.calls[0][1]
    assert arguments["labelId"] == "SENT"
    assert arguments["startHistoryId"] == "old"


def test_history_mixed_events_return_locators_for_refetch_not_payloads() -> None:
    resource = _Resource()
    resource.queue(
        "history",
        {
            "historyId": "102",
            "history": [
                {
                    "id": "101",
                    "messagesAdded": [{"message": {"id": "sent", "threadId": "t1"}}],
                    "messagesDeleted": [{"message": {"id": "removed", "threadId": "t2"}}],
                },
                {
                    "id": "102",
                    "labelsRemoved": [{"message": {"id": "changed", "threadId": "t3"}}],
                },
            ],
        },
    )
    gateway = OfficialGmailGateway(_Service(resource))  # type: ignore[arg-type]

    page = gateway.list_history_page(GmailHistoryCursor("100"), RequestBudget(2, 10))

    assert {locator.message_id for locator in page.locators} == {"sent", "changed"}
    assert page.deleted_message_ids == ("removed",)
    assert page.latest_history_id == "102"
