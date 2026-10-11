from __future__ import annotations

import random
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeVar, cast

from google.auth.credentials import Credentials
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gbrain.domain.connectors import (
    BudgetExhaustedError,
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

_T = TypeVar("_T")
_SAFE_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class ExecutableRequest(Protocol):
    def execute(self, *, num_retries: int = 0) -> object: ...


class GmailMessagesResource(Protocol):
    def list(self, **kwargs: object) -> ExecutableRequest: ...

    def get(self, **kwargs: object) -> ExecutableRequest: ...


class GmailHistoryResource(Protocol):
    def list(self, **kwargs: object) -> ExecutableRequest: ...


class GmailUsersResource(Protocol):
    def getProfile(self, **kwargs: object) -> ExecutableRequest: ...

    def messages(self) -> GmailMessagesResource: ...

    def history(self) -> GmailHistoryResource: ...


class GmailService(Protocol):
    def users(self) -> GmailUsersResource: ...


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    initial_delay_seconds: float = 0.25
    maximum_delay_seconds: float = 8.0
    jitter_ratio: float = 0.2
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 8:
            raise ValueError("max_attempts deve essere compreso tra 1 e 8")
        if self.initial_delay_seconds < 0 or self.maximum_delay_seconds < 0:
            raise ValueError("I ritardi non possono essere negativi")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio deve essere compreso tra 0 e 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds deve essere positivo")


class OfficialGmailGateway:
    """Gateway read-only: espone soltanto profile/list/get/history."""

    scope = GMAIL_READONLY_SCOPE

    def __init__(
        self,
        service: GmailService,
        *,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._random = random_value
        self._monotonic = monotonic

    @classmethod
    def from_credentials(
        cls,
        credentials: Credentials,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> OfficialGmailGateway:
        scopes = frozenset(credentials.scopes or ())
        if scopes != frozenset({GMAIL_READONLY_SCOPE}):
            raise ConnectorError(
                ConnectorErrorCategory.AUTH,
                "Le credenziali Gmail devono avere esclusivamente lo scope readonly",
            )
        service = build(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
            static_discovery=True,
        )
        return cls(cast(GmailService, service), retry_policy=retry_policy)

    def get_profile(self, budget: RequestBudget) -> Mapping[str, Any]:
        response = self._execute(
            lambda: self._service.users().getProfile(userId="me"),
            budget,
            operation="profile.get",
        )
        return _mapping(response, "profile.get")

    def list_sent_page(
        self,
        selection: GmailSelection,
        cursor: GmailCursor | None,
        budget: RequestBudget,
    ) -> ConnectorPage[GmailLocator, GmailCursor]:
        current = cursor or GmailCursor()
        if current.emitted_items >= selection.limit:
            return ConnectorPage((), None, PageState.TRUNCATED_LIMIT)
        item_room = min(selection.limit - current.emitted_items, budget.items_remaining)
        if item_room <= 0:
            raise BudgetExhaustedError("elementi")
        page_size = min(selection.page_size, item_room)
        query = _temporal_query(selection)
        kwargs: dict[str, object] = {
            "userId": "me",
            "labelIds": [GMAIL_SENT_LABEL],
            "q": query,
            "maxResults": page_size,
            "includeSpamTrash": False,
        }
        if current.page_token:
            kwargs["pageToken"] = current.page_token
        response = _mapping(
            self._execute(
                lambda: self._service.users().messages().list(**kwargs),
                budget,
                operation="messages.list",
            ),
            "messages.list",
        )
        raw_messages = response.get("messages", ())
        if not isinstance(raw_messages, list):
            raise ConnectorError(
                ConnectorErrorCategory.PERMANENT,
                "Schema messages.list non valido",
            )
        locators: list[GmailLocator] = []
        for entry in raw_messages:
            entry_mapping = _mapping(entry, "messages.list item")
            message_id = str(entry_mapping.get("id") or "")
            if not message_id:
                raise ConnectorError(
                    ConnectorErrorCategory.PERMANENT,
                    "Elemento messages.list privo di id",
                )
            locators.append(
                GmailLocator(message_id, _optional(entry_mapping.get("threadId")), None)
            )
        accepted = budget.consume_items(len(locators))
        locators = locators[:accepted]
        emitted = current.emitted_items + len(locators)
        next_page_token = _optional(response.get("nextPageToken"))
        if emitted >= selection.limit and next_page_token:
            state = PageState.TRUNCATED_LIMIT
            next_cursor = None
        elif accepted < len(raw_messages):
            state = PageState.PARTIAL_BUDGET
            next_cursor = None
        elif next_page_token:
            state = PageState.COMPLETE
            next_cursor = GmailCursor(next_page_token, emitted)
        else:
            state = PageState.COMPLETE
            next_cursor = None
        return ConnectorPage(tuple(locators), next_cursor, state)

    def fetch_sent_message(
        self,
        locator: GmailLocator,
        budget: RequestBudget,
        *,
        format: str = "full",
    ) -> GmailRawMessage:
        if format not in {"full", "metadata"}:
            raise ValueError("Sono consentiti soltanto format full o metadata")
        response = _mapping(
            self._execute(
                lambda: (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=locator.message_id, format=format)
                ),
                budget,
                operation="messages.get",
            ),
            "messages.get",
        )
        raw = GmailRawMessage.from_api(response)
        if GMAIL_SENT_LABEL not in raw.label_ids:
            raise ConnectorError(
                ConnectorErrorCategory.PERIMETER,
                "Messaggio rifiutato: label SENT assente",
                status_code=None,
            )
        return raw

    def list_history_page(
        self,
        cursor: GmailHistoryCursor,
        budget: RequestBudget,
        *,
        max_results: int = 100,
    ) -> GmailHistoryPage:
        if not 1 <= max_results <= 500:
            raise ValueError("max_results history fuori limite")
        kwargs: dict[str, object] = {
            "userId": "me",
            "startHistoryId": cursor.start_history_id,
            "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
            "labelId": GMAIL_SENT_LABEL,
            "maxResults": max_results,
        }
        if cursor.page_token:
            kwargs["pageToken"] = cursor.page_token
        response = _mapping(
            self._execute(
                lambda: self._service.users().history().list(**kwargs),
                budget,
                operation="history.list",
                history_operation=True,
            ),
            "history.list",
        )
        locators_by_id: dict[str, GmailLocator] = {}
        deleted_ids: set[str] = set()
        history_entries = response.get("history", ())
        if not isinstance(history_entries, list):
            raise ConnectorError(
                ConnectorErrorCategory.PERMANENT,
                "Schema history.list non valido",
            )
        for history_entry in history_entries:
            history_mapping = _mapping(history_entry, "history entry")
            entry_history_id = _optional(history_mapping.get("id"))
            for collection_name in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                collection = history_mapping.get(collection_name, ())
                if not isinstance(collection, list):
                    continue
                for event in collection:
                    event_mapping = _mapping(event, collection_name)
                    message_mapping = _mapping(event_mapping.get("message"), "history message")
                    message_id = str(message_mapping.get("id") or "")
                    if message_id:
                        locators_by_id[message_id] = GmailLocator(
                            message_id=message_id,
                            thread_id=_optional(message_mapping.get("threadId")),
                            history_id=entry_history_id,
                        )
            deleted = history_mapping.get("messagesDeleted", ())
            if isinstance(deleted, list):
                for event in deleted:
                    event_mapping = _mapping(event, "messagesDeleted")
                    message_mapping = _mapping(event_mapping.get("message"), "deleted message")
                    message_id = str(message_mapping.get("id") or "")
                    if message_id:
                        deleted_ids.add(message_id)
        latest_history_id = _optional(response.get("historyId")) or cursor.start_history_id
        next_token = _optional(response.get("nextPageToken"))
        next_cursor = (
            GmailHistoryCursor(cursor.start_history_id, next_token) if next_token else None
        )
        return GmailHistoryPage(
            locators=tuple(locators_by_id.values()),
            deleted_message_ids=tuple(sorted(deleted_ids)),
            next_cursor=next_cursor,
            latest_history_id=latest_history_id,
            state=PageState.COMPLETE,
        )

    def _execute(
        self,
        request_factory: Callable[[], ExecutableRequest],
        budget: RequestBudget,
        *,
        operation: str,
        history_operation: bool = False,
    ) -> object:
        for attempt in range(self._retry.max_attempts):
            budget.consume_request()
            started_at = self._monotonic()
            try:
                response = request_factory().execute(num_retries=0)
                if self._monotonic() - started_at > self._retry.timeout_seconds:
                    raise TimeoutError("deadline locale superata")
                return response
            except ConnectorError:
                raise
            except BaseException as exc:
                error = _classify_error(exc, operation, history_operation=history_operation)
                if not error.retryable or attempt + 1 >= self._retry.max_attempts:
                    raise error from exc
                if budget.requests_remaining <= 0:
                    raise BudgetExhaustedError("richieste") from exc
                delay = _retry_after_seconds(exc)
                if delay is None:
                    base = min(
                        self._retry.maximum_delay_seconds,
                        self._retry.initial_delay_seconds * (2**attempt),
                    )
                    delay = base * (
                        1
                        - self._retry.jitter_ratio
                        + (2 * self._retry.jitter_ratio * self._random())
                    )
                self._sleep(min(delay, self._retry.maximum_delay_seconds))
        raise AssertionError("ciclo retry irraggiungibile")


def _temporal_query(selection: GmailSelection) -> str:
    after = int(selection.after.astimezone(UTC).timestamp())
    before = int(selection.before.astimezone(UTC).timestamp())
    return f"after:{after} before:{before}"


def _classify_error(
    exc: BaseException,
    operation: str,
    *,
    history_operation: bool,
) -> ConnectorError:
    if isinstance(exc, HttpError):
        status = int(getattr(exc.resp, "status", 0) or 0)
        if history_operation and status == 404:
            return ConnectorError(
                ConnectorErrorCategory.HISTORY_EXPIRED,
                "Checkpoint Gmail history scaduto; riconciliazione full necessaria",
                status_code=status,
            )
        if status in {401, 403}:
            return ConnectorError(
                ConnectorErrorCategory.AUTH,
                "Autorizzazione Gmail non valida, revocata o insufficiente",
                status_code=status,
            )
        if status == 429:
            return ConnectorError(
                ConnectorErrorCategory.QUOTA,
                "Quota Gmail temporaneamente esaurita",
                retryable=True,
                status_code=status,
            )
        if status in _SAFE_TRANSIENT_STATUSES:
            return ConnectorError(
                ConnectorErrorCategory.TRANSIENT,
                f"Errore transitorio Gmail durante {operation}",
                retryable=True,
                status_code=status,
            )
        return ConnectorError(
            ConnectorErrorCategory.PERMANENT,
            f"Errore Gmail permanente durante {operation}",
            status_code=status,
        )
    if isinstance(exc, RefreshError):
        return ConnectorError(
            ConnectorErrorCategory.AUTH,
            "Refresh OAuth Gmail fallito o token revocato",
        )
    if isinstance(exc, (TimeoutError, socket.timeout, TransportError, OSError)):
        return ConnectorError(
            ConnectorErrorCategory.TRANSIENT,
            f"Errore di trasporto transitorio durante {operation}",
            retryable=True,
        )
    return ConnectorError(
        ConnectorErrorCategory.PERMANENT,
        f"Risposta o contenuto Gmail non valido durante {operation}",
    )


def _retry_after_seconds(exc: BaseException) -> float | None:
    if not isinstance(exc, HttpError):
        return None
    headers = getattr(exc.resp, "headers", {})
    value = headers.get("retry-after") if isinstance(headers, Mapping) else None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _mapping(value: object, operation: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConnectorError(
            ConnectorErrorCategory.PERMANENT,
            f"Schema Gmail non valido durante {operation}",
        )
    return cast(Mapping[str, Any], value)


def _optional(value: object) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result or None
