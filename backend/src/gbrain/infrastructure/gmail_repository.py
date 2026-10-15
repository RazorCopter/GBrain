from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.domain.gmail import (
    GMAIL_SOURCE_KIND,
    GmailBackfillStateData,
    GmailCheckpointState,
    GmailItemGapData,
    GmailPersistOutcome,
    GmailReconciliationState,
    GmailSyncConfigData,
    NormalizedGmailMessage,
)
from gbrain.domain.models import (
    GmailBackfillState,
    GmailCheckpoint,
    GmailCoverage,
    GmailItemGap,
    ImportJob,
    ReconciliationState,
    SourceItem,
    SourceItemState,
    SourceRevision,
    SourceSyncConfig,
)


class GmailJobStaleSnapshotError(RuntimeError):
    """Sollevata quando la configurazione semantica del job è obsoleta o modificata durante l'esecuzione."""

    pass


class AsyncPostgresGmailRepository:
    """Persistenza PostgreSQL transazionale per l'integrazione worker successiva."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_batch(
        self,
        account_id: uuid.UUID,
        policy_id: uuid.UUID,
        messages: Sequence[NormalizedGmailMessage],
        checkpoint: GmailCheckpointState,
        *,
        config_query_fingerprint: str | None = None,
        removed_external_ids: frozenset[str] = frozenset(),
        out_of_scope_external_ids: frozenset[str] = frozenset(),
        gaps: Sequence[GmailItemGapData] = (),
        job_id: uuid.UUID | None = None,
        examined_count: int | None = None,
    ) -> tuple[GmailPersistOutcome, ...]:
        outcomes: list[GmailPersistOutcome] = []
        now = datetime.now(UTC)
        async with self._session.begin():
            if checkpoint.config_revision is not None:
                config = await self._session.scalar(
                    select(SourceSyncConfig)
                    .where(SourceSyncConfig.connector_account_id == account_id)
                    .with_for_update()
                )
                # Il checkpoint descrive la selezione effettiva. Il fencing usa invece
                # l'identità configurata catturata all'enqueue. L'assenza del valore è
                # compatibilità esplicita per chiamanti/record legacy, nei quali le due
                # fingerprint coincidevano.
                fencing_fingerprint = (
                    config_query_fingerprint
                    if config_query_fingerprint is not None
                    else checkpoint.query_fingerprint
                )
                if (
                    config is None
                    or config.revision != checkpoint.config_revision
                    or config.query_fingerprint != fencing_fingerprint
                ):
                    raise GmailJobStaleSnapshotError("stale_gmail_job_snapshot")
            for message in messages:
                item = await self._session.scalar(
                    select(SourceItem)
                    .where(
                        SourceItem.connector_account_id == account_id,
                        SourceItem.source_kind == GMAIL_SOURCE_KIND,
                        SourceItem.external_id == message.external_id,
                    )
                    .with_for_update()
                )
                inserted = item is None
                if item is None:
                    item = SourceItem(
                        source_kind=GMAIL_SOURCE_KIND,
                        external_id=message.external_id,
                        canonical_hash=message.revision_hash,
                        occurred_at=message.original_date,
                        connector_account_id=account_id,
                        source_state=SourceItemState.ACTIVE,
                        last_observed_at=now,
                    )
                    self._session.add(item)
                    await self._session.flush()
                else:
                    item.connector_account_id = account_id
                    item.source_state = SourceItemState.ACTIVE
                    item.state_reason = None
                    item.last_observed_at = now
                idempotency_key = message.idempotency_key_for(str(account_id))
                existing_revision = await self._session.scalar(
                    select(SourceRevision).where(SourceRevision.idempotency_key == idempotency_key)
                )
                if existing_revision is not None:
                    outcomes.append(
                        GmailPersistOutcome(
                            False, False, True, str(item.id), str(existing_revision.id)
                        )
                    )
                    continue
                if item.canonical_hash == message.revision_hash and not inserted:
                    outcomes.append(GmailPersistOutcome(False, False, True, str(item.id), None))
                    continue
                revision_number = (
                    int(
                        await self._session.scalar(
                            select(func.count(SourceRevision.id)).where(
                                SourceRevision.source_item_id == item.id
                            )
                        )
                        or 0
                    )
                    + 1
                )
                payload = message.provenance_payload()
                evidence = payload.get("evidence", {})
                revision = SourceRevision(
                    source_item_id=item.id,
                    revision_number=revision_number,
                    content_hash=message.revision_hash,
                    normalized_metadata={
                        "external_id": message.external_id,
                        "thread_id": message.thread_id,
                        "rfc_message_id": message.rfc_message_id,
                        "attribution": message.attribution,
                        "untrusted_content": True,
                    },
                    normalized_content=payload,
                    evidence=evidence if isinstance(evidence, dict) else {},
                    pipeline_version=message.pipeline_version,
                    idempotency_key=idempotency_key,
                    source_history_id=message.history_id,
                    storage_ref=None,
                    captured_at=message.acquired_at,
                )
                self._session.add(revision)
                item.canonical_hash = message.revision_hash
                item.occurred_at = message.original_date
                await self._session.flush()
                outcomes.append(
                    GmailPersistOutcome(
                        inserted=inserted,
                        revised=not inserted,
                        unchanged=False,
                        source_item_id=str(item.id),
                        revision_id=str(revision.id),
                    )
                )

                # Se questo messaggio aveva in precedenza un gap aperto, lo marchiamo risolto
                await self._session.execute(
                    update(GmailItemGap)
                    .where(
                        GmailItemGap.connector_account_id == account_id,
                        GmailItemGap.gmail_message_id == message.external_id,
                        GmailItemGap.status == "open",
                    )
                    .values(status="resolved", resolved_at=now, updated_at=now)
                )

            # Persistenza transazionale e idempotente dei gap per-item
            for gap in gaps:
                existing_gap = await self._session.scalar(
                    select(GmailItemGap)
                    .where(
                        GmailItemGap.connector_account_id == account_id,
                        GmailItemGap.gmail_message_id == gap.gmail_message_id,
                        GmailItemGap.config_revision == (checkpoint.config_revision or 0),
                    )
                    .with_for_update()
                )
                if existing_gap is None:
                    new_gap = GmailItemGap(
                        connector_account_id=account_id,
                        source_policy_id=policy_id,
                        import_job_id=job_id,
                        gmail_message_id=gap.gmail_message_id,
                        gmail_thread_id=gap.gmail_thread_id,
                        config_revision=checkpoint.config_revision or 0,
                        query_fingerprint=checkpoint.query_fingerprint,
                        phase=gap.phase,
                        reason_code=gap.reason_code,
                        status="open",
                        occurred_at=gap.occurred_at,
                        details=gap.details,
                    )
                    self._session.add(new_gap)
                else:
                    existing_gap.phase = gap.phase
                    existing_gap.reason_code = gap.reason_code
                    existing_gap.status = "open"
                    existing_gap.resolved_at = None
                    existing_gap.occurred_at = gap.occurred_at
                    existing_gap.details = gap.details
                    if job_id is not None:
                        existing_gap.import_job_id = job_id
                    existing_gap.updated_at = now

            # Messaggi/gap/checkpoint e contatori del job devono essere una singola
            # unita' atomica. In questo modo un crash subito dopo il commit non perde
            # i progressi e la ripresa dal page token non sottostima i contatori.
            inserted_count = sum(1 for outcome in outcomes if outcome.inserted)
            existing_count = sum(1 for outcome in outcomes if outcome.unchanged or outcome.revised)
            not_imported_count = len(gaps)
            if job_id is not None:
                # Il task Gmail e' gia' serializzato dal lock di coda; ``get`` mantiene
                # inoltre compatibilita' con la UoW in-memory usata nei test contrattuali.
                job = await self._session.get(ImportJob, job_id)
                if job is None:
                    raise RuntimeError("gmail_import_job_missing")
                job.imported_items = (job.imported_items or 0) + inserted_count
                job.duplicate_items = (job.duplicate_items or 0) + existing_count
                job.not_imported_items = (job.not_imported_items or 0) + not_imported_count
                job.processed_items = (job.processed_items or 0) + (
                    examined_count
                    if examined_count is not None
                    else len(messages) + not_imported_count
                )

            # Se esiste un record di backfill per questa revisione, aggiorna contatori
            if checkpoint.config_revision is not None:
                backfill_row = await self._session.scalar(
                    select(GmailBackfillState)
                    .where(GmailBackfillState.connector_account_id == account_id)
                    .with_for_update()
                )
                if backfill_row is not None:
                    backfill_row.items_imported = (
                        backfill_row.items_imported or 0
                    ) + inserted_count
                    backfill_row.items_not_imported = (
                        backfill_row.items_not_imported or 0
                    ) + not_imported_count

            await self._mark_source_states(
                account_id,
                removed_external_ids=removed_external_ids,
                out_of_scope_external_ids=out_of_scope_external_ids,
            )
            await self._upsert_checkpoint(account_id, policy_id, checkpoint)
        return tuple(outcomes)

    async def load_checkpoint(
        self,
        account_id: uuid.UUID,
        policy_id: uuid.UUID,
    ) -> GmailCheckpointState:
        row = await self._session.scalar(
            select(GmailCheckpoint).where(
                GmailCheckpoint.connector_account_id == account_id,
                GmailCheckpoint.source_policy_id == policy_id,
            )
        )
        if row is None:
            return GmailCheckpointState(
                account_id=str(account_id),
                policy_id=str(policy_id),
                history_id=None,
                page_token=None,
                coverage_after=None,
                coverage_before=None,
                last_synced_at=None,
                reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
            )
        return GmailCheckpointState(
            account_id=str(account_id),
            policy_id=str(policy_id),
            history_id=row.history_id,
            page_token=row.page_token,
            coverage_after=row.coverage_after,
            coverage_before=row.coverage_before,
            last_synced_at=row.last_sync_at,
            reconciliation_state=GmailReconciliationState(row.reconciliation_state.value),
            last_error_code=row.last_error_code,
            version=row.version,
        )

    async def list_snapshots(self, account_id: uuid.UUID) -> tuple[object, ...]:
        from gbrain.application.gmail_sync import PersistedSnapshot

        rows = await self._session.execute(
            select(SourceItem.external_id, SourceItem.canonical_hash, SourceItem.source_state)
            .where(
                SourceItem.connector_account_id == account_id,
                SourceItem.source_kind == GMAIL_SOURCE_KIND,
            )
            .order_by(SourceItem.external_id)
        )
        return tuple(
            PersistedSnapshot(
                external_id=external_id,
                revision_hash=revision_hash,
                state=state.value,
                excluded=state == SourceItemState.EXCLUDED,
            )
            for external_id, revision_hash, state in rows
        )

    async def mark_reconciliation_required(
        self,
        account_id: uuid.UUID,
        policy_id: uuid.UUID,
        checkpoint: GmailCheckpointState,
    ) -> None:
        async with self._session.begin():
            await self._upsert_checkpoint(account_id, policy_id, checkpoint)

    async def mark_missing(
        self,
        account_id: uuid.UUID,
        policy_id: uuid.UUID,
        missing_external_ids: frozenset[str],
        checkpoint: GmailCheckpointState,
    ) -> tuple[int, int]:
        removed = 0
        preserved = 0
        async with self._session.begin():
            items = tuple(
                await self._session.scalars(
                    select(SourceItem)
                    .where(
                        SourceItem.connector_account_id == account_id,
                        SourceItem.external_id.in_(missing_external_ids),
                    )
                    .with_for_update()
                )
            )
            for item in items:
                if item.source_state == SourceItemState.EXCLUDED:
                    preserved += 1
                    continue
                item.source_state = SourceItemState.REMOVED
                item.state_reason = "not_observed_during_full_reconciliation"
                removed += 1
            await self._upsert_checkpoint(account_id, policy_id, checkpoint)
        return removed, preserved

    async def _mark_source_states(
        self,
        account_id: uuid.UUID,
        *,
        removed_external_ids: frozenset[str],
        out_of_scope_external_ids: frozenset[str],
    ) -> None:
        transitions = (
            (removed_external_ids, SourceItemState.REMOVED, "gmail_history_deleted"),
            (
                out_of_scope_external_ids,
                SourceItemState.OUT_OF_SCOPE,
                "gmail_sent_label_absent",
            ),
        )
        for external_ids, state, reason in transitions:
            if not external_ids:
                continue
            items = tuple(
                await self._session.scalars(
                    select(SourceItem)
                    .where(
                        SourceItem.connector_account_id == account_id,
                        SourceItem.external_id.in_(external_ids),
                    )
                    .with_for_update()
                )
            )
            for item in items:
                if item.source_state == SourceItemState.EXCLUDED:
                    continue
                item.source_state = state
                item.state_reason = reason

    async def _upsert_checkpoint(
        self,
        account_id: uuid.UUID,
        policy_id: uuid.UUID,
        state: GmailCheckpointState,
    ) -> None:
        checkpoint = await self._session.get(GmailCheckpoint, account_id, with_for_update=True)
        reconciliation_state = ReconciliationState(state.reconciliation_state.value)
        if checkpoint is None:
            checkpoint = GmailCheckpoint(
                connector_account_id=account_id,
                source_policy_id=policy_id,
                history_id=state.history_id,
                page_token=state.page_token,
                coverage_after=state.coverage_after,
                coverage_before=state.coverage_before,
                last_sync_at=state.last_synced_at,
                reconciliation_state=reconciliation_state,
                last_error_code=state.last_error_code,
                version=state.version,
                config_revision=state.config_revision,
                query_fingerprint=state.query_fingerprint,
            )
            self._session.add(checkpoint)
            return
        checkpoint.source_policy_id = policy_id
        checkpoint.history_id = state.history_id
        checkpoint.page_token = state.page_token
        checkpoint.coverage_after = state.coverage_after
        checkpoint.coverage_before = state.coverage_before
        checkpoint.last_sync_at = state.last_synced_at
        checkpoint.reconciliation_state = reconciliation_state
        checkpoint.last_error_code = state.last_error_code
        checkpoint.version = state.version
        checkpoint.config_revision = state.config_revision
        checkpoint.query_fingerprint = state.query_fingerprint

    async def load_sync_config(
        self, account_id: uuid.UUID, *, for_update: bool = False
    ) -> SourceSyncConfig | None:
        query = select(SourceSyncConfig).where(SourceSyncConfig.connector_account_id == account_id)
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def upsert_sync_config(
        self, account_id: uuid.UUID, data: GmailSyncConfigData
    ) -> SourceSyncConfig:
        config = await self._session.scalar(
            select(SourceSyncConfig)
            .where(SourceSyncConfig.connector_account_id == account_id)
            .with_for_update()
        )
        if config is None:
            config = SourceSyncConfig(
                connector_account_id=account_id,
                revision=data.revision,
                history_after=data.history_after,
                history_before=data.history_before,
                selection_label=data.selection_label,
                query_filter=data.query_filter,
                item_limit=data.item_limit,
                request_budget=data.request_budget,
                batch_size=data.batch_size,
                update_policy=data.update_policy,
                query_fingerprint=data.query_fingerprint,
            )
            self._session.add(config)
            return config

        config.revision = data.revision
        config.history_after = data.history_after
        config.history_before = data.history_before
        config.selection_label = data.selection_label
        config.query_filter = data.query_filter
        config.item_limit = data.item_limit
        config.request_budget = data.request_budget
        config.batch_size = data.batch_size
        config.update_policy = data.update_policy
        config.query_fingerprint = data.query_fingerprint
        return config

    async def load_backfill_state(self, account_id: uuid.UUID) -> GmailBackfillState | None:
        return await self._session.scalar(
            select(GmailBackfillState).where(GmailBackfillState.connector_account_id == account_id)
        )

    async def upsert_backfill_state(
        self, account_id: uuid.UUID, data: GmailBackfillStateData
    ) -> GmailBackfillState:
        state = await self._session.scalar(
            select(GmailBackfillState)
            .where(GmailBackfillState.connector_account_id == account_id)
            .with_for_update()
        )
        if state is None:
            state = GmailBackfillState(
                connector_account_id=account_id,
                config_revision=data.config_revision,
                query_fingerprint=data.query_fingerprint,
                history_after=data.history_after,
                history_before=data.history_before,
                page_token=data.page_token,
                status=data.status,
                items_imported=data.items_imported,
                last_run_at=data.last_run_at,
                last_error_code=data.last_error_code,
            )
            self._session.add(state)
            return state

        state.config_revision = data.config_revision
        state.query_fingerprint = data.query_fingerprint
        state.history_after = data.history_after
        state.history_before = data.history_before
        state.page_token = data.page_token
        state.status = data.status
        state.items_imported = data.items_imported
        state.last_run_at = data.last_run_at
        state.last_error_code = data.last_error_code
        return state

    async def load_coverage(self, account_id: uuid.UUID) -> GmailCoverage | None:
        return await self._session.scalar(
            select(GmailCoverage).where(GmailCoverage.connector_account_id == account_id)
        )

    async def update_coverage(
        self,
        account_id: uuid.UUID,
        *,
        covered_after: datetime | None,
        covered_before: datetime | None,
        query_fingerprint: str | None = None,
        config_revision: int | None = None,
        is_legacy: bool = False,
    ) -> GmailCoverage:
        coverage = await self._session.scalar(
            select(GmailCoverage)
            .where(GmailCoverage.connector_account_id == account_id)
            .with_for_update()
        )
        now = datetime.now(UTC)
        if coverage is None:
            coverage = GmailCoverage(
                connector_account_id=account_id,
                covered_after=covered_after,
                covered_before=covered_before,
                query_fingerprint=query_fingerprint,
                config_revision=config_revision,
                is_legacy=is_legacy,
                last_completed_at=now,
            )
            self._session.add(coverage)
            return coverage

        same_selection = (
            coverage.query_fingerprint == query_fingerprint
            and coverage.config_revision == config_revision
        )
        if not same_selection:
            # Una copertura appartiene a una precisa selezione/revisione: unirla con
            # un'altra fingerprint dichiarerebbe coperti intervalli mai scansionati.
            coverage.covered_after = covered_after
            coverage.covered_before = covered_before
        else:
            if covered_after is not None:
                if coverage.covered_after is None or covered_after < coverage.covered_after:
                    coverage.covered_after = covered_after
            if covered_before is not None:
                if coverage.covered_before is None or covered_before > coverage.covered_before:
                    coverage.covered_before = covered_before

        coverage.query_fingerprint = query_fingerprint
        coverage.config_revision = config_revision
        coverage.is_legacy = is_legacy
        coverage.last_completed_at = now

        if is_legacy:
            coverage.status = "legacy"
            coverage.has_gaps = False
            coverage.missing_count = 0
        else:
            open_gaps_count = (
                await self._session.scalar(
                    select(func.count(GmailItemGap.id)).where(
                        GmailItemGap.connector_account_id == account_id,
                        GmailItemGap.status == "open",
                    )
                )
                or 0
            )
            coverage.missing_count = int(open_gaps_count)
            coverage.has_gaps = open_gaps_count > 0
            coverage.status = "complete_with_gaps" if open_gaps_count > 0 else "completed"

        return coverage

    async def list_gaps(
        self,
        account_id: uuid.UUID,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[GmailItemGap], int]:
        query = select(GmailItemGap).where(GmailItemGap.connector_account_id == account_id)
        if status:
            query = query.where(GmailItemGap.status == status)
        count_query = select(func.count(GmailItemGap.id)).where(
            GmailItemGap.connector_account_id == account_id
        )
        if status:
            count_query = count_query.where(GmailItemGap.status == status)

        total = await self._session.scalar(count_query) or 0
        query = query.order_by(GmailItemGap.occurred_at.desc(), GmailItemGap.created_at.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)
        items = (await self._session.scalars(query)).all()
        return tuple(items), int(total)

    async def count_gaps(self, account_id: uuid.UUID) -> tuple[int, int]:
        open_count = (
            await self._session.scalar(
                select(func.count(GmailItemGap.id)).where(
                    GmailItemGap.connector_account_id == account_id,
                    GmailItemGap.status == "open",
                )
            )
            or 0
        )
        resolved_count = (
            await self._session.scalar(
                select(func.count(GmailItemGap.id)).where(
                    GmailItemGap.connector_account_id == account_id,
                    GmailItemGap.status == "resolved",
                )
            )
            or 0
        )
        return int(open_count), int(resolved_count)


def checkpoint_from_model(model: GmailCheckpoint) -> GmailCheckpointState:
    return GmailCheckpointState(
        account_id=str(model.connector_account_id),
        policy_id=str(model.source_policy_id),
        history_id=model.history_id,
        page_token=model.page_token,
        coverage_after=model.coverage_after,
        coverage_before=model.coverage_before,
        last_synced_at=model.last_sync_at,
        reconciliation_state=GmailReconciliationState(model.reconciliation_state.value),
        last_error_code=model.last_error_code,
        version=model.version,
        config_revision=model.config_revision,
        query_fingerprint=model.query_fingerprint,
    )
