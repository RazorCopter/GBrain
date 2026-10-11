from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from gbrain.domain.connectors import (
    ConnectorError,
    ConnectorErrorCategory,
    ConnectorPage,
    PageState,
    RequestBudget,
)
from gbrain.domain.gmail import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SENT_LABEL,
    GmailCursor,
    GmailHistoryCursor,
    GmailHistoryPage,
    GmailLocator,
    GmailRawMessage,
    GmailSelection,
)


class FakeGmailGateway:
    scope = GMAIL_READONLY_SCOPE

    def __init__(self, fixture: Mapping[str, Any], *, page_size: int = 2) -> None:
        messages = fixture.get("messages", ())
        if not isinstance(messages, list):
            raise ValueError("Fixture messages non valida")
        self.profile = copy.deepcopy(fixture.get("profile", {}))
        self.messages: dict[str, dict[str, Any]] = {
            str(message["id"]): copy.deepcopy(message)
            for message in messages
            if isinstance(message, dict)
        }
        self.page_size = page_size
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, list[ConnectorError]] = {}
        self.history_pages: list[dict[str, Any]] = []
        self.history_expired = False

    def queue_failure(self, operation: str, error: ConnectorError) -> None:
        self.failures.setdefault(operation, []).append(error)

    def get_profile(self, budget: RequestBudget) -> Mapping[str, Any]:
        budget.consume_request()
        self._raise_failure("profile.get")
        self.calls.append(("profile.get", {"userId": "me"}))
        return copy.deepcopy(self.profile)

    def list_sent_page(
        self,
        selection: GmailSelection,
        cursor: GmailCursor | None,
        budget: RequestBudget,
    ) -> ConnectorPage[GmailLocator, GmailCursor]:
        budget.consume_request()
        self._raise_failure("messages.list")
        current = cursor or GmailCursor()
        query = (
            f"after:{int(selection.after.timestamp())} before:{int(selection.before.timestamp())}"
        )
        self.calls.append(
            (
                "messages.list",
                {
                    "userId": "me",
                    "labelIds": [GMAIL_SENT_LABEL],
                    "q": query,
                    "pageToken": current.page_token,
                },
            )
        )
        sent = [
            message
            for message in self.messages.values()
            if GMAIL_SENT_LABEL in message.get("labelIds", ())
            and selection.after.timestamp() * 1000
            <= int(message.get("internalDate", 0))
            < selection.before.timestamp() * 1000
        ]
        sent.sort(key=lambda item: (int(item.get("internalDate", 0)), str(item["id"])))
        offset = int(current.page_token or 0)
        page_size = min(self.page_size, selection.page_size, budget.items_remaining)
        if page_size <= 0:
            from gbrain.domain.connectors import BudgetExhaustedError

            raise BudgetExhaustedError("elementi")
        room = selection.limit - current.emitted_items
        selected = sent[offset : offset + min(page_size, room)]
        budget.consume_items(len(selected))
        next_offset = offset + len(selected)
        more = next_offset < len(sent)
        reached_limit = current.emitted_items + len(selected) >= selection.limit
        state = PageState.TRUNCATED_LIMIT if reached_limit and more else PageState.COMPLETE
        next_cursor = None
        if more and not reached_limit:
            next_cursor = GmailCursor(str(next_offset), current.emitted_items + len(selected))
        return ConnectorPage(
            tuple(
                GmailLocator(str(item["id"]), str(item.get("threadId") or ""), None)
                for item in selected
            ),
            next_cursor,
            state,
        )

    def fetch_sent_message(
        self,
        locator: GmailLocator,
        budget: RequestBudget,
        *,
        format: str = "full",
    ) -> GmailRawMessage:
        budget.consume_request()
        self._raise_failure("messages.get")
        self.calls.append(
            (
                "messages.get",
                {"userId": "me", "id": locator.message_id, "format": format},
            )
        )
        message = self.messages.get(locator.message_id)
        if message is None:
            raise ConnectorError(
                ConnectorErrorCategory.PERMANENT,
                "Messaggio fixture non trovato",
                status_code=404,
            )
        raw = GmailRawMessage.from_api(message)
        if GMAIL_SENT_LABEL not in raw.label_ids:
            raise ConnectorError(
                ConnectorErrorCategory.PERIMETER,
                "Messaggio fixture non SENT",
            )
        return raw

    def list_history_page(
        self,
        cursor: GmailHistoryCursor,
        budget: RequestBudget,
        *,
        max_results: int = 100,
    ) -> GmailHistoryPage:
        del max_results
        budget.consume_request()
        self._raise_failure("history.list")
        self.calls.append(
            (
                "history.list",
                {
                    "userId": "me",
                    "startHistoryId": cursor.start_history_id,
                    "labelId": GMAIL_SENT_LABEL,
                    "pageToken": cursor.page_token,
                },
            )
        )
        if self.history_expired:
            raise ConnectorError(
                ConnectorErrorCategory.HISTORY_EXPIRED,
                "Checkpoint fixture scaduto",
                status_code=404,
            )
        index = int(cursor.page_token or 0)
        if index >= len(self.history_pages):
            return GmailHistoryPage((), (), None, cursor.start_history_id, PageState.COMPLETE)
        page = self.history_pages[index]
        changed_ids = tuple(str(item) for item in page.get("changed", ()))
        deleted_ids = tuple(str(item) for item in page.get("deleted", ()))
        next_cursor = (
            GmailHistoryCursor(cursor.start_history_id, str(index + 1))
            if index + 1 < len(self.history_pages)
            else None
        )
        return GmailHistoryPage(
            locators=tuple(
                GmailLocator(message_id, history_id=str(page.get("history_id", "")))
                for message_id in changed_ids
            ),
            deleted_message_ids=deleted_ids,
            next_cursor=next_cursor,
            latest_history_id=str(page.get("history_id", cursor.start_history_id)),
            state=PageState.COMPLETE,
        )

    def _raise_failure(self, operation: str) -> None:
        failures = self.failures.get(operation)
        if failures:
            raise failures.pop(0)
