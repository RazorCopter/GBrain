from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.domain.gmail import (
    GMAIL_SOURCE_KIND,
    GmailCheckpointState,
    GmailPersistOutcome,
    GmailReconciliationState,
    NormalizedGmailMessage,
)
from gbrain.domain.models import (
    GmailCheckpoint,
    ReconciliationState,
    SourceItem,
    SourceItemState,
    SourceRevision,
)


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
    ) -> tuple[GmailPersistOutcome, ...]:
        outcomes: list[GmailPersistOutcome] = []
        now = datetime.now(UTC)
        async with self._session.begin():
            for message in messages:
                item = await self._session.scalar(
                    select(SourceItem)
                    .where(
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
                existing_revision = await self._session.scalar(
                    select(SourceRevision).where(
                        SourceRevision.idempotency_key == message.idempotency_key
                    )
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
                    idempotency_key=message.idempotency_key,
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
            await self._upsert_checkpoint(account_id, policy_id, checkpoint)
        return tuple(outcomes)

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
    )
