from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from gbrain.application.gmail_mime import MimeLimits, normalize_gmail_message
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
    GmailItemGapData,
    GmailLocator,
    GmailPersistOutcome,
    GmailRawMessage,
    GmailReconciliationState,
    GmailSelection,
    GmailSyncMode,
    GmailSyncResult,
    NormalizedGmailMessage,
    resolve_sync_mode,
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
        *,
        removed_external_ids: frozenset[str] = frozenset(),
        out_of_scope_external_ids: frozenset[str] = frozenset(),
        gaps: Sequence[GmailItemGapData] = (),
    ) -> tuple[GmailPersistOutcome, ...]:
        """Persiste batch, stati sorgente e checkpoint nella stessa transazione durevole."""

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
        limits: MimeLimits | None = None,
    ) -> None:
        if gateway.scope != GMAIL_READONLY_SCOPE:
            raise ValueError("Il gateway Gmail deve usare lo scope readonly esatto")
        self._gateway = gateway
        self._owner_aliases = tuple(owner_aliases)
        self._now = now
        self._limits = limits

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
            limits=self._limits,
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
        # La pagina è il confine di commit. Ridurla al batch massimo evita di
        # avanzare un page token prima che tutti gli elementi della pagina siano durevoli.
        effective_selection = GmailSelection(
            after=selection.after,
            before=selection.before,
            limit=selection.limit,
            page_size=min(selection.page_size, self._batch_size),
        )
        cursor = GmailCursor(checkpoint.page_token) if checkpoint.page_token else None
        counters = _Counters()
        state = PageState.COMPLETE
        current_checkpoint = checkpoint
        try:
            while True:
                page = self._connector.enumerate_page(effective_selection, cursor, budget)
                counters.pages += 1
                state = page.state
                page_messages: list[NormalizedGmailMessage] = []
                out_of_scope_ids: set[str] = set()
                page_gaps: list[GmailItemGapData] = []
                for locator in page.items:
                    try:
                        page_messages.append(
                            self._connector.normalize_item(
                                self._connector.fetch_item(locator, budget)
                            )
                        )
                    except ConnectorError as exc:
                        if exc.category == ConnectorErrorCategory.PERIMETER:
                            counters.rejected += 1
                            out_of_scope_ids.add(locator.message_id)
                            continue
                        if exc.category == ConnectorErrorCategory.PARSE:
                            counters.not_imported += 1
                            reason = getattr(exc, "reason_code", None) or "MIME_PARSE_ERROR"
                            gap = GmailItemGapData(
                                gmail_message_id=locator.message_id,
                                phase="mime_parse",
                                reason_code=reason,
                                details={"error_code": reason, "safe_message": exc.safe_message},
                                occurred_at=self._connector._now(),
                            )
                            page_gaps.append(gap)
                            counters.gaps.append(gap)
                            continue
                        raise
                current_checkpoint = _checkpoint_after_page(
                    current_checkpoint,
                    effective_selection,
                    page.next_cursor,
                    page_messages,
                    self._connector._now(),
                )
                # Anche una pagina contenente soli rifiuti o gap deve avanzare solo in
                # una transazione checkpoint esplicita.
                self._commit_batch(
                    page_messages,
                    current_checkpoint,
                    counters,
                    out_of_scope_external_ids=frozenset(out_of_scope_ids),
                    gaps=tuple(page_gaps),
                )
                cursor = page.next_cursor
                if cursor is None:
                    break
        except BudgetExhaustedError:
            # Nessun checkpoint viene avanzato per una pagina non completamente
            # fetchata: il replay riparte dal suo token precedente senza skip.
            state = PageState.PARTIAL_BUDGET
        # Se la scansione completa termina ma il checkpoint non ha history_id
        # (es. nessuna email inviata nel range), tentiamo di leggere il reale historyId dal profilo Google
        if state == PageState.COMPLETE and current_checkpoint.history_id is None:
            try:
                profile = self._connector._gateway.get_profile(budget)
                profile_history = getattr(profile, "get", lambda _: None)("historyId")
                if profile_history:
                    current_checkpoint = GmailCheckpointState(
                        account_id=current_checkpoint.account_id,
                        policy_id=current_checkpoint.policy_id,
                        history_id=str(profile_history),
                        page_token=current_checkpoint.page_token,
                        coverage_after=current_checkpoint.coverage_after,
                        coverage_before=current_checkpoint.coverage_before,
                        last_synced_at=current_checkpoint.last_synced_at,
                        reconciliation_state=current_checkpoint.reconciliation_state,
                        last_error_code=current_checkpoint.last_error_code,
                        version=current_checkpoint.version,
                    )
                    self._commit_batch((), current_checkpoint, counters)
            except Exception:
                import logging

                logging.getLogger(__name__).debug(
                    "Lettura profile fallback per historyId non riuscita"
                )
        return counters.result(GmailSyncMode.FULL, state, current_checkpoint)

    def sync(
        self,
        selection: GmailSelection,
        checkpoint: GmailCheckpointState,
        budget: RequestBudget,
        requested_mode: GmailSyncMode | str | None = None,
    ) -> GmailSyncResult:
        """Sincronizzazione unificata e autoritativa.

        Risolve la modalità reale secondo lo stato del checkpoint (mai cieca fiducia nel chiamante).
        Se la history è scaduta durante l'incrementale, esegue fallback controllato a full_reconcile.
        """
        mode = resolve_sync_mode(checkpoint, requested_mode)
        if mode == GmailSyncMode.FULL:
            return self.full_sync(selection, checkpoint, budget)
        if mode == GmailSyncMode.RECONCILIATION:
            return self.full_reconcile(selection, checkpoint, budget)

        # Modalità INCREMENTAL con fallback controllato a riconciliazione se history scaduto
        try:
            result = self.incremental_sync(checkpoint, budget, selection=selection)
            if "full_reconciliation_required" in result.detail_codes:
                reconcile_checkpoint = result.checkpoint
                reconcile_result = self.full_reconcile(selection, reconcile_checkpoint, budget)
                return GmailSyncResult(
                    mode=GmailSyncMode.RECONCILIATION,
                    persisted_items=result.persisted_items + reconcile_result.persisted_items,
                    revised_items=result.revised_items + reconcile_result.revised_items,
                    unchanged_items=result.unchanged_items + reconcile_result.unchanged_items,
                    rejected_items=result.rejected_items + reconcile_result.rejected_items,
                    removed_items=result.removed_items + reconcile_result.removed_items,
                    pages=result.pages + reconcile_result.pages,
                    state=reconcile_result.state,
                    checkpoint=reconcile_result.checkpoint,
                    detail_codes=(
                        "history_expired_fallback_reconciled",
                        *reconcile_result.detail_codes,
                    ),
                )
            return result
        except ConnectorError as exc:
            if exc.category == ConnectorErrorCategory.HISTORY_EXPIRED:
                reconcile_checkpoint = checkpoint.require_full_reconciliation(
                    "gmail_history_expired"
                )
                self._uow.mark_reconciliation_required(reconcile_checkpoint)
                reconcile_result = self.full_reconcile(selection, reconcile_checkpoint, budget)
                return GmailSyncResult(
                    mode=GmailSyncMode.RECONCILIATION,
                    persisted_items=reconcile_result.persisted_items,
                    revised_items=reconcile_result.revised_items,
                    unchanged_items=reconcile_result.unchanged_items,
                    rejected_items=reconcile_result.rejected_items,
                    removed_items=reconcile_result.removed_items,
                    pages=reconcile_result.pages,
                    state=reconcile_result.state,
                    checkpoint=reconcile_result.checkpoint,
                    detail_codes=(
                        "history_expired_fallback_reconciled",
                        *reconcile_result.detail_codes,
                    ),
                )
            raise

    def incremental_sync(
        self,
        checkpoint: GmailCheckpointState,
        budget: RequestBudget,
        *,
        selection: GmailSelection | None = None,
    ) -> GmailSyncResult:
        if not checkpoint.history_id:
            if selection is not None:
                return self.full_sync(selection, checkpoint, budget)
            raise ValueError("L'incrementale richiede history_id")
        cursor: GmailHistoryCursor | None = GmailHistoryCursor(checkpoint.history_id)
        counters = _Counters()
        current_checkpoint = checkpoint
        try:
            while cursor is not None:
                page = self._connector._gateway.list_history_page(cursor, budget)
                counters.pages += 1
                batch: list[NormalizedGmailMessage] = []
                out_of_scope_ids: set[str] = set()
                for locator in page.locators:
                    try:
                        # Ogni evento history viene rifetchato e riverificato SENT dal gateway.
                        raw = self._connector.fetch_item(locator, budget)
                        batch.append(self._connector.normalize_item(raw))
                    except ConnectorError as exc:
                        if exc.category == ConnectorErrorCategory.PERIMETER:
                            counters.rejected += 1
                            out_of_scope_ids.add(locator.message_id)
                            continue
                        if (
                            exc.category == ConnectorErrorCategory.PERMANENT
                            and exc.status_code == 404
                        ):
                            continue
                        raise
                next_checkpoint = GmailCheckpointState(
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
                # I chunk intermedi usano il checkpoint precedente: un crash li
                # ripete idempotentemente. Solo il commit finale avanza historyId.
                for offset in range(0, len(batch), self._batch_size):
                    chunk = batch[offset : offset + self._batch_size]
                    is_final_chunk = offset + self._batch_size >= len(batch)
                    chunk_checkpoint = next_checkpoint if is_final_chunk else current_checkpoint
                    self._commit_batch(
                        chunk,
                        chunk_checkpoint,
                        counters,
                        removed_external_ids=(
                            frozenset(page.deleted_message_ids) if is_final_chunk else frozenset()
                        ),
                        out_of_scope_external_ids=(
                            frozenset(out_of_scope_ids) if is_final_chunk else frozenset()
                        ),
                    )
                if not batch:
                    self._commit_batch(
                        (),
                        next_checkpoint,
                        counters,
                        removed_external_ids=frozenset(page.deleted_message_ids),
                        out_of_scope_external_ids=frozenset(out_of_scope_ids),
                    )
                counters.removed += len(page.deleted_message_ids)
                current_checkpoint = next_checkpoint
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
            snapshot_index = {
                item.external_id: item for item in self._uow.list_snapshots(checkpoint.account_id)
            }
            for locator in page.items:
                observed.add(locator.message_id)
                snapshot = snapshot_index.get(locator.message_id)
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
            persisted_snapshots = self._uow.list_snapshots(checkpoint.account_id)
            missing = frozenset(
                item.external_id
                for item in persisted_snapshots
                if item.external_id not in observed and item.state == "active"
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
        *,
        removed_external_ids: frozenset[str] = frozenset(),
        out_of_scope_external_ids: frozenset[str] = frozenset(),
        gaps: Sequence[GmailItemGapData] = (),
    ) -> None:
        outcomes = self._uow.persist_batch(
            checkpoint.account_id,
            batch,
            checkpoint,
            removed_external_ids=removed_external_ids,
            out_of_scope_external_ids=out_of_scope_external_ids,
            gaps=gaps,
        )
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
    not_imported: int = 0
    gaps: list[GmailItemGapData] = field(default_factory=list)

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
            not_imported_items=self.not_imported,
            gaps=tuple(self.gaps),
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
        history_id=(
            messages[-1].history_id or checkpoint.history_id if messages else checkpoint.history_id
        ),
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
        self.gaps: dict[tuple[str, str], GmailItemGapData] = {}
        self.commit_count = 0

    def persist_batch(
        self,
        account_id: str,
        messages: Sequence[NormalizedGmailMessage],
        checkpoint: GmailCheckpointState,
        *,
        removed_external_ids: frozenset[str] = frozenset(),
        out_of_scope_external_ids: frozenset[str] = frozenset(),
        gaps: Sequence[GmailItemGapData] = (),
    ) -> tuple[GmailPersistOutcome, ...]:
        staged_messages = dict(self.messages)
        staged_revisions = dict(self.revisions)
        staged_states = dict(self.states)
        outcomes: list[GmailPersistOutcome] = []
        for message in messages:
            identity = (account_id, message.external_id)
            existing = staged_messages.get(identity)
            idempotency_key = message.idempotency_key_for(account_id)
            revision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))
            if idempotency_key in staged_revisions:
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
            staged_revisions[idempotency_key] = message
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
        for external_id in removed_external_ids:
            identity = (account_id, external_id)
            staged_states[identity] = "excluded" if identity in self.excluded else "removed"
        for external_id in out_of_scope_external_ids:
            identity = (account_id, external_id)
            staged_states[identity] = "excluded" if identity in self.excluded else "out_of_scope"
        for gap in gaps:
            self.gaps[(account_id, gap.gmail_message_id)] = gap
        for message in messages:
            gap_key = (account_id, message.external_id)
            if gap_key in self.gaps:
                existing_gap = self.gaps[gap_key]
                self.gaps[gap_key] = GmailItemGapData(
                    gmail_message_id=existing_gap.gmail_message_id,
                    phase=existing_gap.phase,
                    reason_code=existing_gap.reason_code,
                    details=existing_gap.details,
                    occurred_at=existing_gap.occurred_at,
                    gmail_thread_id=existing_gap.gmail_thread_id,
                    status="resolved",
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
