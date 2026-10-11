from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from gbrain.application.gmail_mime import normalize_gmail_message
from gbrain.domain.connectors import (
    BudgetExhaustedError,
    ConnectionStatus,
    ConnectorCapabilities,
    ConnectorError,
    ConnectorErrorCategory,
    ConnectorPage,
    PageState,
    PreviewResult,
    ReconcileResult,
    RequestBudget,
)
from gbrain.domain.gmail import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SOURCE_KIND,
    GmailCheckpointState,
    GmailCursor,
    GmailHistoryCursor,
    GmailHistoryPage,
    GmailLocator,
    GmailPersistOutcome,
    GmailRawMessage,
    GmailReconciliationState,
    GmailSelection,
    GmailSyncMode,
    GmailSyncResult,
    NormalizedGmailMessage,
)


class GmailReadGateway(Protocol):
    scope: str

    def get_profile(self, budget: RequestBudget) -> object: ...

    def list_sent_page(
        self,
        selection: GmailSelection,
        cursor: GmailCursor | None,
        budget: RequestBudget,
    ) -> ConnectorPage[GmailLocator, GmailCursor]: ...

    def fetch_sent_message(
        self,
        locator: GmailLocator,
        budget: RequestBudget,
        *,
        format: str = "full",
    ) -> GmailRawMessage: ...

    def list_history_page(
        self,
        cursor: GmailHistoryCursor,
        budget: RequestBudget,
        *,
        max_results: int = 100,
    ) -> GmailHistoryPage: ...


@dataclass(frozen=True, slots=True)
class PersistedSnapshot:
    external_id: str
    revision_hash: str
    state: str
    excluded: bool


class GmailUnitOfWork(Protocol):
    def persist_batch(
        self,
        account_id: str,
        messages: Sequence[NormalizedGmailMessage],
        checkpoint: GmailCheckpointState,
    ) -> tuple[GmailPersistOutcome, ...]:
        """Persiste batch e checkpoint nella stessa transazione durevole."""

    def mark_reconciliation_required(self, checkpoint: GmailCheckpointState) -> None: ...

    def list_snapshots(self, account_id: str) -> tuple[PersistedSnapshot, ...]: ...

    def mark_missing(
        self,
        account_id: str,
        missing_external_ids: frozenset[str],
        *,
        reason: str,
        checkpoint: GmailCheckpointState,
    ) -> tuple[int, int]:
        """Marca rimossi/out-of-scope; non cancella e preserva excluded."""


class GmailSentConnector:
    """Connettore deterministico; nessun contenuto email influenza la policy."""

    def __init__(
        self,
        gateway: GmailReadGateway,
        owner_aliases: Sequence[str],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if gateway.scope != GMAIL_READONLY_SCOPE:
            raise ValueError("Il gateway Gmail deve usare lo scope readonly esatto")
        self._gateway = gateway
        self._owner_aliases = tuple(owner_aliases)
        self._now = now

    def test_connection(self, budget: RequestBudget) -> ConnectionStatus:
        profile = self._gateway.get_profile(budget)
        email_address = getattr(profile, "get", lambda _: None)("emailAddress")
        return ConnectionStatus(
            connected=True,
            account_reference=str(email_address) if email_address else None,
            checked_at=self._now(),
            detail_code="gmail_profile_ok",
        )

    def inspect_capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_kind=GMAIL_SOURCE_KIND,
            read_only=True,
            pagination=True,
            incremental=True,
            reconciliation=True,
            max_page_size=500,
            concurrency=1,
            operations=(
                "test_connection",
                "inspect_capabilities",
                "preview_selection",
                "enumerate_page",
                "fetch_item",
                "normalize_item",
                "checkpoint",
                "reconcile",
            ),
        )

    def preview_selection(self, selection: GmailSelection, budget: RequestBudget) -> PreviewResult:
        cursor: GmailCursor | None = None
        selected = 0
        pages = 0
        state = PageState.COMPLETE
        next_present = False
        try:
            while True:
                page = self.enumerate_page(selection, cursor, budget)
                pages += 1
                selected += len(page.items)
                state = page.state
                cursor = page.next_cursor
                next_present = cursor is not None
                if cursor is None:
                    break
        except BudgetExhaustedError:
            state = PageState.PARTIAL_BUDGET
            next_present = cursor is not None
        return PreviewResult(
            selected_items=selected,
            pages_read=pages,
            state=state,
            requests_used=budget.requests_used,
            next_cursor_present=next_present,
        )

    def enumerate_page(
        self,
        selection: GmailSelection,
        cursor: GmailCursor | None,
        budget: RequestBudget,
    ) -> ConnectorPage[GmailLocator, GmailCursor]:
        return self._gateway.list_sent_page(selection, cursor, budget)

    def fetch_item(self, locator: GmailLocator, budget: RequestBudget) -> GmailRawMessage:
        return self._gateway.fetch_sent_message(locator, budget, format="full")

    def normalize_item(self, raw_data: GmailRawMessage) -> NormalizedGmailMessage:
        return normalize_gmail_message(
            raw_data,
            self._owner_aliases,
            acquired_at=self._now(),
        )

    def checkpoint(self, state: GmailCheckpointState) -> GmailCheckpointState:
        return state

    def reconcile(
        self,
        selection: GmailSelection,
        checkpoint: GmailCheckpointState,
        budget: RequestBudget,
    ) -> ReconcileResult:
        del checkpoint
        preview = self.preview_selection(selection, budget)
        return ReconcileResult(
            observed_items=preview.selected_items,
            unchanged_items=0,
            revised_items=0,
            removed_items=0,
            out_of_scope_items=0,
            excluded_items_preserved=0,
            state=preview.state,
            detail_codes=("preview_only_reconcile_contract",),
        )


class GmailSyncEngine:
    def __init__(
        self,
        connector: GmailSentConnector,
        unit_of_work: GmailUnitOfWork,
        *,
        batch_size: int = 25,
        after_batch_commit: Callable[[int], None] | None = None,
    ) -> None:
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size fuori limite")
        self._connector = connector
        self._uow = unit_of_work
        self._batch_size = batch_size
        self._after_batch_commit = after_batch_commit

    def full_sync(
        self,
        selection: GmailSelection,
        checkpoint: GmailCheckpointState,
        budget: RequestBudget,
    ) -> GmailSyncResult:
        cursor = GmailCursor(checkpoint.page_token) if checkpoint.page_token else None
        batch: list[NormalizedGmailMessage] = []
        counters = _Counters()
        state = PageState.COMPLETE
        current_checkpoint = checkpoint
        try:
            while True:
                page = self._connector.enumerate_page(selection, cursor, budget)
                counters.pages += 1
                state = page.state
                for locator in page.items:
                    try:
                        normalized = self._connector.normalize_item(
                            self._connector.fetch_item(locator, budget)
                        )
                    except ConnectorError as exc:
                        if exc.category == ConnectorErrorCategory.PERIMETER:
                            counters.rejected += 1
                            continue
                        raise
                    batch.append(normalized)
                    if len(batch) >= self._batch_size:
                        current_checkpoint = _checkpoint_after_page(
                            current_checkpoint,
                            selection,
                            page.next_cursor,
                            batch,
                            self._connector._now(),
                        )
                        self._commit_batch(batch, current_checkpoint, counters)
                        batch = []
                cursor = page.next_cursor
                if cursor is None:
                    if batch:
                        current_checkpoint = _checkpoint_after_page(
                            current_checkpoint,
                            selection,
                            None,
                            batch,
                            self._connector._now(),
                        )
                        self._commit_batch(batch, current_checkpoint, counters)
                    break
        except BudgetExhaustedError:
            state = PageState.PARTIAL_BUDGET
            if batch:
                current_checkpoint = _checkpoint_after_page(
                    current_checkpoint,
                    selection,
                    cursor,
                    batch,
                    self._connector._now(),
                )
                self._commit_batch(batch, current_checkpoint, counters)
        return counters.result(GmailSyncMode.FULL, state, current_checkpoint)

    def incremental_sync(
        self,
        checkpoint: GmailCheckpointState,
        budget: RequestBudget,
    ) -> GmailSyncResult:
        if not checkpoint.history_id:
            raise ValueError("L'incrementale richiede history_id")
        cursor: GmailHistoryCursor | None = GmailHistoryCursor(checkpoint.history_id)
        counters = _Counters()
        current_checkpoint = checkpoint
        try:
            while cursor is not None:
                page = self._connector._gateway.list_history_page(cursor, budget)
                counters.pages += 1
                batch: list[NormalizedGmailMessage] = []
                for locator in page.locators:
                    try:
                        # Ogni evento history viene rifetchato e riverificato SENT dal gateway.
                        raw = self._connector.fetch_item(locator, budget)
                        batch.append(self._connector.normalize_item(raw))
                    except ConnectorError as exc:
                        if exc.category == ConnectorErrorCategory.PERIMETER:
                            counters.rejected += 1
                            continue
                        if (
                            exc.category == ConnectorErrorCategory.PERMANENT
                            and exc.status_code == 404
                        ):
                            continue
                        raise
                current_checkpoint = GmailCheckpointState(
                    account_id=checkpoint.account_id,
                    policy_id=checkpoint.policy_id,
                    history_id=page.latest_history_id,
                    page_token=page.next_cursor.page_token if page.next_cursor else None,
                    coverage_after=checkpoint.coverage_after,
                    coverage_before=checkpoint.coverage_before,
                    last_synced_at=self._connector._now(),
                    reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
                    version=checkpoint.version,
                )
                if batch:
                    self._commit_batch(batch, current_checkpoint, counters)
                elif page.next_cursor is None:
                    # Anche un history page vuoto richiede una transazione checkpoint durevole.
                    self._uow.persist_batch(checkpoint.account_id, (), current_checkpoint)
                cursor = page.next_cursor
        except ConnectorError as exc:
            if exc.category != ConnectorErrorCategory.HISTORY_EXPIRED:
                raise
            current_checkpoint = checkpoint.require_full_reconciliation("gmail_history_expired")
            self._uow.mark_reconciliation_required(current_checkpoint)
            return counters.result(
                GmailSyncMode.INCREMENTAL,
                PageState.PARTIAL_BUDGET,
                current_checkpoint,
                detail_codes=("full_reconciliation_required",),
            )
        except BudgetExhaustedError:
            return counters.result(
                GmailSyncMode.INCREMENTAL,
                PageState.PARTIAL_BUDGET,
                current_checkpoint,
            )
        return counters.result(GmailSyncMode.INCREMENTAL, PageState.COMPLETE, current_checkpoint)

    def full_reconcile(
        self,
        selection: GmailSelection,
        checkpoint: GmailCheckpointState,
        budget: RequestBudget,
    ) -> GmailSyncResult:
        observed: set[str] = set()
        cursor: GmailCursor | None = None
        counters = _Counters()
        current_checkpoint = checkpoint
        state = PageState.COMPLETE
        while True:
            try:
                page = self._connector.enumerate_page(selection, cursor, budget)
            except BudgetExhaustedError:
                state = PageState.PARTIAL_BUDGET
                break
            counters.pages += 1
            state = page.state
            batch: list[NormalizedGmailMessage] = []
            for locator in page.items:
                observed.add(locator.message_id)
                snapshots = {
                    snapshot.external_id: snapshot
                    for snapshot in self._uow.list_snapshots(checkpoint.account_id)
                }
                snapshot = snapshots.get(locator.message_id)
                if snapshot is not None and snapshot.state == "active":
                    counters.unchanged += 1
                    continue
                try:
                    batch.append(
                        self._connector.normalize_item(self._connector.fetch_item(locator, budget))
                    )
                except ConnectorError as exc:
                    if exc.category == ConnectorErrorCategory.PERIMETER:
                        counters.rejected += 1
                        continue
                    raise
            current_checkpoint = GmailCheckpointState(
                account_id=checkpoint.account_id,
                policy_id=checkpoint.policy_id,
                history_id=(batch[-1].history_id if batch else checkpoint.history_id),
                page_token=page.next_cursor.page_token if page.next_cursor else None,
                coverage_after=selection.after,
                coverage_before=selection.before,
                last_synced_at=self._connector._now(),
                reconciliation_state=(
                    GmailReconciliationState.RUNNING
                    if page.next_cursor
                    else GmailReconciliationState.COMPLETE
                ),
                version=checkpoint.version,
            )
            if batch:
                self._commit_batch(batch, current_checkpoint, counters)
            cursor = page.next_cursor
            if cursor is None:
                break
        if state == PageState.COMPLETE:
            snapshots = self._uow.list_snapshots(checkpoint.account_id)
            missing = frozenset(
                snapshot.external_id
                for snapshot in snapshots
                if snapshot.external_id not in observed and snapshot.state == "active"
            )
            removed, excluded_preserved = self._uow.mark_missing(
                checkpoint.account_id,
                missing,
                reason="not_observed_during_full_reconciliation",
                checkpoint=current_checkpoint,
            )
            counters.removed += removed
            counters.excluded_preserved += excluded_preserved
        return counters.result(GmailSyncMode.RECONCILIATION, state, current_checkpoint)

    def _commit_batch(
        self,
        batch: Sequence[NormalizedGmailMessage],
        checkpoint: GmailCheckpointState,
        counters: _Counters,
    ) -> None:
        outcomes = self._uow.persist_batch(checkpoint.account_id, batch, checkpoint)
        for outcome in outcomes:
            counters.persisted += int(outcome.inserted)
            counters.revised += int(outcome.revised)
            counters.unchanged += int(outcome.unchanged)
        if self._after_batch_commit:
            # Hook di test eseguito dopo il commit: un crash qui deve essere replay-safe.
            self._after_batch_commit(len(outcomes))


@dataclass(slots=True)
class _Counters:
    persisted: int = 0
    revised: int = 0
    unchanged: int = 0
    rejected: int = 0
    removed: int = 0
    excluded_preserved: int = 0
    pages: int = 0

    def result(
        self,
        mode: GmailSyncMode,
        state: PageState,
        checkpoint: GmailCheckpointState,
        *,
        detail_codes: tuple[str, ...] = (),
    ) -> GmailSyncResult:
        return GmailSyncResult(
            mode=mode,
            persisted_items=self.persisted,
            revised_items=self.revised,
            unchanged_items=self.unchanged,
            rejected_items=self.rejected,
            removed_items=self.removed,
            pages=self.pages,
            state=state,
            checkpoint=checkpoint,
            detail_codes=detail_codes,
        )


def _checkpoint_after_page(
    checkpoint: GmailCheckpointState,
    selection: GmailSelection,
    cursor: GmailCursor | None,
    messages: Sequence[NormalizedGmailMessage],
    synced_at: datetime,
) -> GmailCheckpointState:
    return GmailCheckpointState(
        account_id=checkpoint.account_id,
        policy_id=checkpoint.policy_id,
        history_id=messages[-1].history_id or checkpoint.history_id,
        page_token=cursor.page_token if cursor else None,
        coverage_after=selection.after,
        coverage_before=selection.before,
        last_synced_at=synced_at,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
        version=checkpoint.version,
    )


class InMemoryGmailUnitOfWork:
    """Fake transazionale fixture-first; utile anche per verificare crash boundary."""

    def __init__(self) -> None:
        self.messages: dict[tuple[str, str], NormalizedGmailMessage] = {}
        self.revisions: dict[str, NormalizedGmailMessage] = {}
        self.states: dict[tuple[str, str], str] = {}
        self.excluded: set[tuple[str, str]] = set()
        self.checkpoints: dict[str, GmailCheckpointState] = {}
        self.commit_count = 0

    def persist_batch(
        self,
        account_id: str,
        messages: Sequence[NormalizedGmailMessage],
        checkpoint: GmailCheckpointState,
    ) -> tuple[GmailPersistOutcome, ...]:
        staged_messages = dict(self.messages)
        staged_revisions = dict(self.revisions)
        staged_states = dict(self.states)
        outcomes: list[GmailPersistOutcome] = []
        for message in messages:
            identity = (account_id, message.external_id)
            existing = staged_messages.get(identity)
            revision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, message.idempotency_key))
            if message.idempotency_key in staged_revisions:
                outcomes.append(
                    GmailPersistOutcome(False, False, True, message.external_id, revision_id)
                )
                staged_states[identity] = "active"
                continue
            if existing is not None and existing.revision_hash == message.revision_hash:
                outcomes.append(GmailPersistOutcome(False, False, True, message.external_id, None))
                staged_states[identity] = "active"
                continue
            staged_messages[identity] = message
            staged_revisions[message.idempotency_key] = message
            staged_states[identity] = "active"
            outcomes.append(
                GmailPersistOutcome(
                    inserted=existing is None,
                    revised=existing is not None,
                    unchanged=False,
                    source_item_id=message.external_id,
                    revision_id=revision_id,
                )
            )
        self.messages = staged_messages
        self.revisions = staged_revisions
        self.states = staged_states
        self.checkpoints[account_id] = checkpoint
        self.commit_count += 1
        return tuple(outcomes)

    def mark_reconciliation_required(self, checkpoint: GmailCheckpointState) -> None:
        self.checkpoints[checkpoint.account_id] = checkpoint
        self.commit_count += 1

    def list_snapshots(self, account_id: str) -> tuple[PersistedSnapshot, ...]:
        return tuple(
            PersistedSnapshot(
                external_id=external_id,
                revision_hash=message.revision_hash,
                state=self.states.get((stored_account, external_id), "active"),
                excluded=(stored_account, external_id) in self.excluded,
            )
            for (stored_account, external_id), message in sorted(self.messages.items())
            if stored_account == account_id
        )

    def mark_missing(
        self,
        account_id: str,
        missing_external_ids: frozenset[str],
        *,
        reason: str,
        checkpoint: GmailCheckpointState,
    ) -> tuple[int, int]:
        del reason
        removed = 0
        preserved = 0
        for external_id in missing_external_ids:
            identity = (account_id, external_id)
            if identity in self.excluded:
                self.states[identity] = "excluded"
                preserved += 1
            else:
                self.states[identity] = "removed"
                removed += 1
        self.checkpoints[account_id] = checkpoint
        self.commit_count += 1
        return removed, preserved
