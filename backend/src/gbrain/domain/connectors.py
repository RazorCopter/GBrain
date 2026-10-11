from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeVar

SelectionT = TypeVar("SelectionT")
CursorT = TypeVar("CursorT")
LocatorT = TypeVar("LocatorT")
RawT = TypeVar("RawT")
NormalizedT = TypeVar("NormalizedT")
CheckpointT = TypeVar("CheckpointT")
ReconcileT = TypeVar("ReconcileT")


class ConnectorErrorCategory(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    TRANSIENT = "transient"
    HISTORY_EXPIRED = "history_expired"
    PERMANENT = "permanent"
    PARSE = "parse"
    PERIMETER = "perimeter"
    BUDGET = "budget"
    CHECKPOINT_CONFLICT = "checkpoint_conflict"


class ConnectorError(RuntimeError):
    def __init__(
        self,
        category: ConnectorErrorCategory,
        safe_message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.category = category
        self.safe_message = safe_message
        self.retryable = retryable
        self.status_code = status_code


class BudgetExhaustedError(ConnectorError):
    def __init__(self, resource: str) -> None:
        super().__init__(
            ConnectorErrorCategory.BUDGET,
            f"Budget del connettore esaurito: {resource}",
        )
        self.resource = resource


class PageState(StrEnum):
    COMPLETE = "complete"
    PARTIAL_BUDGET = "partial_budget"
    TRUNCATED_LIMIT = "truncated_limit"


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    connected: bool
    account_reference: str | None
    checked_at: datetime
    detail_code: str


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    source_kind: str
    read_only: bool
    pagination: bool
    incremental: bool
    reconciliation: bool
    max_page_size: int
    concurrency: int
    operations: tuple[str, ...]


@dataclass(slots=True)
class RequestBudget:
    max_requests: int
    max_items: int
    requests_used: int = 0
    items_used: int = 0

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise ValueError("max_requests deve essere positivo")
        if self.max_items < 1:
            raise ValueError("max_items deve essere positivo")
        if self.requests_used < 0 or self.items_used < 0:
            raise ValueError("I contatori di budget non possono essere negativi")
        if self.requests_used > self.max_requests or self.items_used > self.max_items:
            raise ValueError("I contatori iniziali superano il budget")

    @property
    def requests_remaining(self) -> int:
        return self.max_requests - self.requests_used

    @property
    def items_remaining(self) -> int:
        return self.max_items - self.items_used

    def consume_request(self) -> None:
        if self.requests_remaining <= 0:
            raise BudgetExhaustedError("richieste")
        self.requests_used += 1

    def consume_items(self, count: int) -> int:
        if count < 0:
            raise ValueError("count non può essere negativo")
        accepted = min(count, self.items_remaining)
        self.items_used += accepted
        return accepted


@dataclass(frozen=True, slots=True)
class ConnectorPage[LocatorT, CursorT]:
    items: tuple[LocatorT, ...]
    next_cursor: CursorT | None
    state: PageState


@dataclass(frozen=True, slots=True)
class PreviewResult:
    selected_items: int
    pages_read: int
    state: PageState
    requests_used: int
    next_cursor_present: bool


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    observed_items: int
    unchanged_items: int
    revised_items: int
    removed_items: int
    out_of_scope_items: int
    excluded_items_preserved: int
    state: PageState
    detail_codes: tuple[str, ...] = field(default_factory=tuple)


class ConnectorContract(
    Protocol[SelectionT, CursorT, LocatorT, RawT, NormalizedT, CheckpointT, ReconcileT]
):
    """Contratto semantico comune; non espone oggetti SDK proprietari."""

    def test_connection(self, budget: RequestBudget) -> ConnectionStatus: ...

    def inspect_capabilities(self) -> ConnectorCapabilities: ...

    def preview_selection(self, selection: SelectionT, budget: RequestBudget) -> PreviewResult: ...

    def enumerate_page(
        self,
        selection: SelectionT,
        cursor: CursorT | None,
        budget: RequestBudget,
    ) -> ConnectorPage[LocatorT, CursorT]: ...

    def fetch_item(self, locator: LocatorT, budget: RequestBudget) -> RawT: ...

    def normalize_item(self, raw_data: RawT) -> NormalizedT: ...

    def checkpoint(self, state: CheckpointT) -> CheckpointT: ...

    def reconcile(
        self,
        selection: SelectionT,
        checkpoint: CheckpointT,
        budget: RequestBudget,
    ) -> ReconcileT: ...
