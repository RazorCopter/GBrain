from __future__ import annotations

import json
import logging
import random
import re
import socket
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeVar, cast

from google.auth.credentials import Credentials
from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from httplib2 import Http, HttpLib2Error
from requests.exceptions import RequestException as RequestsError
from urllib3.exceptions import HTTPError as Urllib3Error

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
from gbrain.infrastructure.gmail_rate_limiter import GmailRateLimiter

_T = TypeVar("_T")
_SAFE_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
logger = logging.getLogger(__name__)


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
class CallMetric:
    method: str
    status: str
    category: str


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
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
        account_id: uuid.UUID | str | None = None,
        rate_limiter: GmailRateLimiter | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._account_id = account_id
        self._rate_limiter = rate_limiter
        self._is_cancelled = is_cancelled
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._random = random_value
        self._monotonic = monotonic
        self._metrics: list[CallMetric] = []

    def get_metrics(self) -> tuple[CallMetric, ...]:
        return tuple(self._metrics)

    def get_call_count(
        self,
        *,
        method: str | None = None,
        status: str | None = None,
        category: str | None = None,
    ) -> int:
        return sum(
            1
            for m in self._metrics
            if (method is None or m.method == method)
            and (status is None or m.status == status)
            and (category is None or m.category == category)
        )

    @classmethod
    def from_credentials(
        cls,
        credentials: Credentials,
        *,
        account_id: uuid.UUID | str | None = None,
        rate_limiter: GmailRateLimiter | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> OfficialGmailGateway:
        scopes_value = getattr(credentials, "scopes", None)
        scopes = frozenset(scopes_value or ())
        if scopes != frozenset({GMAIL_READONLY_SCOPE}):
            raise ConnectorError(
                ConnectorErrorCategory.AUTH,
                "Le credenziali Gmail devono avere esclusivamente lo scope readonly",
            )
        # Normalizzazione difensiva di expiry: google.auth richiede naive UTC
        if credentials.expiry is not None and credentials.expiry.tzinfo is not None:
            credentials.expiry = credentials.expiry.astimezone(UTC).replace(tzinfo=None)
        effective_retry = retry_policy or RetryPolicy()
        authorized_http = AuthorizedHttp(
            credentials,
            http=Http(timeout=effective_retry.timeout_seconds),
        )
        service = build(
            "gmail",
            "v1",
            http=authorized_http,
            cache_discovery=False,
            static_discovery=True,
        )
        return cls(
            cast(GmailService, service),
            account_id=account_id,
            rate_limiter=rate_limiter,
            is_cancelled=is_cancelled,
            retry_policy=effective_retry,
        )

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
        if selection.query_filter:
            # Il filtro è validato e trattato come sintassi di ricerca Gmail intenzionale;
            # label e limiti temporali restano imposti separatamente dal backend.
            query = f"{query} ({selection.query_filter})"
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
        seen_ids: set[str] = set()
        for entry in raw_messages:
            entry_mapping = _mapping(entry, "messages.list item")
            message_id = str(entry_mapping.get("id") or "")
            if not message_id:
                raise ConnectorError(
                    ConnectorErrorCategory.PERMANENT,
                    "Elemento messages.list privo di id",
                )
            if message_id in seen_ids:
                continue
            seen_ids.add(message_id)
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
            if self._is_cancelled is not None and self._is_cancelled():
                raise TimeoutError("Operazione Gmail interrotta per cancellazione richiesta")
            budget.consume_request()
            if self._rate_limiter is not None and self._account_id is not None:
                self._rate_limiter.acquire(self._account_id, is_cancelled=self._is_cancelled)
            started_at = self._monotonic()
            try:
                response = request_factory().execute(num_retries=0)
                if self._monotonic() - started_at > self._retry.timeout_seconds:
                    raise TimeoutError("deadline locale superata")
                self._metrics.append(CallMetric(method=operation, status="success", category="ok"))
                return response
            except ConnectorError as err:
                self._metrics.append(
                    CallMetric(method=operation, status="error", category=err.category.value)
                )
                raise
            except Exception as exc:
                error = _classify_error(exc, operation, history_operation=history_operation)
                if (
                    self._rate_limiter is not None
                    and self._account_id is not None
                    and error.retry_after_seconds
                ):
                    if hasattr(self._rate_limiter, "block_for"):
                        self._rate_limiter.block_for(self._account_id, error.retry_after_seconds)
                    else:
                        self._rate_limiter.apply_cooldown(
                            self._account_id, error.retry_after_seconds
                        )
                self._metrics.append(
                    CallMetric(method=operation, status="error", category=error.category.value)
                )
                logger.warning(
                    "Operazione Gmail '%s' fallita (tentativo %d/%d, %s): %s",
                    operation,
                    attempt + 1,
                    self._retry.max_attempts,
                    exc.__class__.__name__,
                    error.safe_message,
                )
                if not error.retryable or attempt + 1 >= self._retry.max_attempts:
                    raise error from exc
                if budget.requests_remaining <= 0:
                    raise BudgetExhaustedError("richieste") from exc
                delay = error.retry_after_seconds
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
                sleep_duration = min(delay, self._retry.maximum_delay_seconds)
                if self._is_cancelled is None:
                    self._sleep(sleep_duration)
                else:
                    remaining_sleep = sleep_duration
                    while remaining_sleep > 0:
                        if self._is_cancelled():
                            raise TimeoutError(
                                "Operazione Gmail interrotta per cancellazione richiesta"
                            ) from exc
                        step = min(0.05, remaining_sleep)
                        self._sleep(step)
                        remaining_sleep -= step
        raise ConnectorError(ConnectorErrorCategory.TRANSIENT, "Tentativi esauriti")


def _temporal_query(selection: GmailSelection) -> str:
    after = int(selection.after.astimezone(UTC).timestamp())
    before = int(selection.before.astimezone(UTC).timestamp())
    return f"after:{after} before:{before}"


def _sanitize_error_text(text: str) -> str:
    if not text:
        return ""
    sanitized = text
    # 1. URL di callback completi
    sanitized = re.sub(
        r"https?://[^\s]*(?:oauth/callback|oauth2callback|/callback)[^\s]*",
        "[REDACTED_CALLBACK_URL]",
        sanitized,
        flags=re.IGNORECASE,
    )
    # 2. Query string dagli altri URL (https://...?... -> https://...)
    sanitized = re.sub(r"(https?://[^\s\?]+)\?[^\s]+", r"\1", sanitized)
    # 3. Access token Bearer e Google ya29
    sanitized = re.sub(
        r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", sanitized, flags=re.IGNORECASE
    )
    sanitized = re.sub(r"ya29\.[A-Za-z0-9_\-]+", "[REDACTED_TOKEN]", sanitized)
    sanitized = re.sub(
        r'(?:access_token)\s*[:=]\s*["\']?[^"\'\s&]+["\']?',
        "access_token=[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    # 4. Refresh token (Google 1//... o 1%2F%2F... o parametro)
    sanitized = re.sub(r"1//[A-Za-z0-9_\-]+", "[REDACTED_REFRESH_TOKEN]", sanitized)
    sanitized = re.sub(r"1%2F%2F[A-Za-z0-9_\-]+", "[REDACTED_REFRESH_TOKEN]", sanitized)
    sanitized = re.sub(
        r'(?:refresh_token)\s*[:=]\s*["\']?[^"\'\s&]+["\']?',
        "refresh_token=[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    # 5. Authorization code (Google 4/... o 4%2F... o parametro code)
    sanitized = re.sub(r"4/[A-Za-z0-9_\-]+", "[REDACTED_AUTH_CODE]", sanitized)
    sanitized = re.sub(r"4%2F[A-Za-z0-9_\-]+", "[REDACTED_AUTH_CODE]", sanitized)
    sanitized = re.sub(
        r'(?:(?<=[?&\s])|(?<=^)|(?<=["\']))code\s*[:=]\s*["\']?[^"\'\s&]+["\']?',
        "code=[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    # 6. Client secret, password, client_id, secret, redirect_uri
    sanitized = re.sub(
        r'(client_secret|secret|password|client_id|redirect_uri)\s*[:=]\s*["\']?[^"\'\s&]+["\']?',
        r"\1=[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    # 7. Cookie e header/token di sessione
    sanitized = re.sub(
        r'(cookie|set-cookie)\s*[:=]\s*["\']?[^"\'\r\n]+["\']?',
        r"\1=[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r'session\s*[:=]\s*["\']?[A-Za-z0-9_\-\.]+["\']?',
        "session=[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    # 8. Indirizzi email
    sanitized = re.sub(
        r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[REDACTED_EMAIL]", sanitized
    )
    # 9. Comprimi spazi multipli e ritorni a capo
    sanitized = " ".join(sanitized.split())
    if len(sanitized) > 300:
        sanitized = sanitized[:297] + "..."
    return sanitized


sanitize_error_text = _sanitize_error_text


def _iter_exception_chain(exc: BaseException | None) -> list[BaseException]:
    chain: list[BaseException] = []
    visited: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        chain.append(current)
        for arg in getattr(current, "args", ()):
            if isinstance(arg, BaseException) and id(arg) not in visited:
                chain.extend(_iter_exception_chain(arg))
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException) and id(reason) not in visited:
            chain.extend(_iter_exception_chain(reason))
        current = current.__cause__ or current.__context__
    return chain


_TRANSIENT_EXCEPTION_CLASSES: tuple[type[BaseException], ...] = (
    TimeoutError,
    socket.timeout,
    socket.gaierror,
    ConnectionError,
    OSError,
    HttpLib2Error,
    TransportError,
    Urllib3Error,
    RequestsError,
)

_TRANSIENT_CLASS_KEYWORDS = (
    "nameresolutionerror",
    "maxretryerror",
    "connectionerror",
    "transporterror",
    "timeouterror",
    "connecttimeout",
    "readtimeout",
    "servernotfounderror",
    "gaierror",
    "sslerror",
    "newconnectionerror",
)

_TRANSIENT_MESSAGE_PATTERNS = (
    "nameresolutionerror",
    "temporary failure in name resolution",
    "failed to establish a new connection",
    "getaddrinfo failed",
    "name or service not known",
    "nodename nor servname provided",
    "connection refused",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "remote disconnected",
    "timed out",
    "timeout",
    "servernotfounderror",
    "unable to find the server at",
    "certificate_verify_failed",
    "ssl: ",
    "certificate verify failed",
)


def is_transient_transport_error(exc: BaseException) -> bool:
    """Riconosce se l'eccezione o la catena causale è un errore temporaneo di trasporto/DNS/TLS."""
    chain = _iter_exception_chain(exc)
    for err in chain:
        if isinstance(err, _TRANSIENT_EXCEPTION_CLASSES):
            return True
        name = err.__class__.__name__.lower()
        if any(keyword in name for keyword in _TRANSIENT_CLASS_KEYWORDS):
            return True
        msg = str(err).lower()
        if any(pattern in msg for pattern in _TRANSIENT_MESSAGE_PATTERNS):
            return True
    return False


MAX_RETRY_AFTER_SECONDS: float = 300.0
_QUOTA_REASONS: frozenset[str] = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
)


def _extract_google_error_reasons(exc: HttpError) -> list[str]:
    try:
        content = getattr(exc, "content", None)
        if not content:
            return []
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        data = json.loads(text)
        if not isinstance(data, dict):
            return []
        error_dict = data.get("error")
        if not isinstance(error_dict, dict):
            return []
        reasons: list[str] = []
        errors = error_dict.get("errors")
        if isinstance(errors, list):
            for err in errors:
                if isinstance(err, dict):
                    reason = err.get("reason")
                    if reason:
                        reasons.append(str(reason))
        details = error_dict.get("details")
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict):
                    reason = detail.get("reason")
                    if reason:
                        reasons.append(str(reason))
        return reasons
    except Exception:
        return []


def _classify_error(
    exc: BaseException,
    operation: str,
    *,
    history_operation: bool = False,
) -> ConnectorError:
    if isinstance(exc, HttpError):
        status = int(getattr(exc.resp, "status", 0) or 0)
        reason_phrase = str(getattr(exc.resp, "reason", "") or "").strip()
        raw_reason = getattr(exc, "reason", "") or ""
        google_reasons = _extract_google_error_reasons(exc)
        google_reason = google_reasons[0] if google_reasons else None
        sanitized_reason = _sanitize_error_text(str(raw_reason))
        retry_after = _retry_after_seconds(exc)
        http_desc = (
            f"Google HTTP {status} {reason_phrase}".strip()
            if reason_phrase
            else f"Google HTTP {status}"
        )
        detail_suffix = (
            f": {sanitized_reason}"
            if sanitized_reason and sanitized_reason.lower() != reason_phrase.lower()
            else ""
        )
        if history_operation and status == 404:
            return ConnectorError(
                ConnectorErrorCategory.HISTORY_EXPIRED,
                f"Checkpoint Gmail history scaduto durante {operation} ({http_desc}); riconciliazione full necessaria",
                status_code=status,
                reason_code=google_reason,
                retry_after_seconds=retry_after,
            )
        is_quota = bool(set(google_reasons) & _QUOTA_REASONS)
        if status == 403:
            if is_quota:
                quota_reason = next(r for r in google_reasons if r in _QUOTA_REASONS)
                return ConnectorError(
                    ConnectorErrorCategory.QUOTA,
                    f"Quota Gmail temporaneamente esaurita durante {operation} ({http_desc}{detail_suffix})",
                    retryable=True,
                    status_code=status,
                    reason_code=quota_reason,
                    retry_after_seconds=retry_after,
                )
            return ConnectorError(
                ConnectorErrorCategory.AUTH,
                f"Autorizzazione Gmail non valida, revocata o insufficiente durante {operation} ({http_desc}{detail_suffix})",
                status_code=status,
                reason_code=google_reason or "forbidden",
                retry_after_seconds=retry_after,
            )
        if status == 401:
            return ConnectorError(
                ConnectorErrorCategory.AUTH,
                f"Autorizzazione Gmail non valida, revocata o insufficiente durante {operation} ({http_desc}{detail_suffix})",
                status_code=status,
                reason_code=google_reason or "unauthorized",
                retry_after_seconds=retry_after,
            )
        if status == 429:
            quota_reason = next(
                (r for r in google_reasons if r in _QUOTA_REASONS), "rateLimitExceeded"
            )
            return ConnectorError(
                ConnectorErrorCategory.QUOTA,
                f"Quota Gmail temporaneamente esaurita durante {operation} ({http_desc}{detail_suffix})",
                retryable=True,
                status_code=status,
                reason_code=quota_reason,
                retry_after_seconds=retry_after,
            )
        if status in _SAFE_TRANSIENT_STATUSES:
            return ConnectorError(
                ConnectorErrorCategory.TRANSIENT,
                f"Errore transitorio Gmail durante {operation} ({http_desc}{detail_suffix})",
                retryable=True,
                status_code=status,
                reason_code=google_reason,
                retry_after_seconds=retry_after,
            )
        return ConnectorError(
            ConnectorErrorCategory.PERMANENT,
            f"Errore permanente Gmail durante {operation} ({http_desc}{detail_suffix})",
            status_code=status,
            reason_code=google_reason,
            retry_after_seconds=retry_after,
        )
    if is_transient_transport_error(exc):
        sanitized = _sanitize_error_text(str(exc))
        detail = f": {sanitized}" if sanitized else ""
        return ConnectorError(
            ConnectorErrorCategory.TRANSIENT,
            f"Errore di trasporto transitorio durante {operation} ({exc.__class__.__name__}{detail})",
            retryable=True,
        )
    if isinstance(exc, RefreshError):
        sanitized = _sanitize_error_text(str(exc))
        detail = f": {sanitized}" if sanitized else ""
        return ConnectorError(
            ConnectorErrorCategory.AUTH,
            f"Refresh OAuth Gmail fallito o token revocato durante {operation} ({exc.__class__.__name__}{detail})",
            retryable=False,
        )
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ConnectorError(
            ConnectorErrorCategory.TRANSIENT,
            f"Timeout di rete durante {operation} ({exc.__class__.__name__})",
            retryable=True,
        )
    if isinstance(exc, (TransportError, OSError, HttpLib2Error, socket.error)):
        sanitized = _sanitize_error_text(str(exc))
        detail = f": {sanitized}" if sanitized else ""
        return ConnectorError(
            ConnectorErrorCategory.TRANSIENT,
            f"Errore di trasporto transitorio durante {operation} ({exc.__class__.__name__}{detail})",
            retryable=True,
        )
    if isinstance(exc, GoogleAuthError):
        sanitized = _sanitize_error_text(str(exc))
        detail = f": {sanitized}" if sanitized else ""
        return ConnectorError(
            ConnectorErrorCategory.AUTH,
            f"Errore autenticazione Google durante {operation} ({exc.__class__.__name__}{detail})",
        )
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError)):
        sanitized = _sanitize_error_text(str(exc))
        detail = f": {sanitized}" if sanitized else ""
        return ConnectorError(
            ConnectorErrorCategory.PARSE,
            f"Risposta o formato Gmail non valido durante {operation} ({exc.__class__.__name__}{detail})",
        )
    sanitized = _sanitize_error_text(str(exc))
    detail = f": {sanitized}" if sanitized else ""
    return ConnectorError(
        ConnectorErrorCategory.PERMANENT,
        f"Errore Gmail durante {operation} ({exc.__class__.__name__}{detail})",
    )


def _retry_after_seconds(
    exc: BaseException,
    *,
    max_cap: float = MAX_RETRY_AFTER_SECONDS,
) -> float | None:
    if not isinstance(exc, HttpError):
        return None
    headers = getattr(exc.resp, "headers", {})
    if not isinstance(headers, Mapping):
        return None
    raw_val: object = None
    for k, v in headers.items():
        if k.lower() == "retry-after":
            raw_val = v
            break
    if raw_val is None:
        return None
    val_str = str(raw_val).strip()
    seconds: float | None = None
    try:
        seconds = max(0.0, float(val_str))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(val_str)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds is None:
        return None
    return min(seconds, max_cap)


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
