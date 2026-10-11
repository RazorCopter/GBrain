from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select

from gbrain.application.gmail_oauth import (
    GmailOAuthError,
    decrypt_credentials,
    encrypt_credentials,
    refresh_credentials,
)
from gbrain.application.gmail_service import GmailServiceError
from gbrain.application.gmail_sync import GmailSentConnector, PersistedSnapshot
from gbrain.config import Settings
from gbrain.domain.connectors import (
    ConnectorError,
    ConnectorErrorCategory,
    PageState,
    RequestBudget,
)
from gbrain.domain.gmail import (
    GmailCheckpointState,
    GmailCursor,
    GmailHistoryCursor,
    GmailReconciliationState,
    GmailSelection,
    GmailSyncMode,
)
from gbrain.domain.models import (
    ConnectorAccount,
    ConnectorAccountStatus,
    ImportJob,
    ImportJobStatus,
)
from gbrain.infrastructure.database import Database
from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway
from gbrain.infrastructure.gmail_repository import AsyncPostgresGmailRepository

GatewayBuilder = Callable[[ConnectorAccount, Settings], OfficialGmailGateway]


class GmailJobTransientError(RuntimeError):
    pass


class GmailJobPermanentError(RuntimeError):
    pass


def _gateway(account: ConnectorAccount, settings: Settings) -> OfficialGmailGateway:
    return OfficialGmailGateway.from_credentials(decrypt_credentials(account, settings))


def _selection(snapshot: dict[str, Any]) -> GmailSelection:
    try:
        return GmailSelection(
            after=datetime.fromisoformat(str(snapshot["after"])),
            before=datetime.fromisoformat(str(snapshot["before"])),
            limit=int(snapshot["item_limit"]),
            page_size=min(int(snapshot["batch_size"]), 500),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GmailJobPermanentError("Snapshot policy Gmail non valido") from exc


def _checkpoint_payload(value: GmailCheckpointState, **extra: object) -> dict[str, object]:
    return {
        "history_id": value.history_id,
        "page_token": value.page_token,
        "coverage_after": value.coverage_after.isoformat() if value.coverage_after else None,
        "coverage_before": value.coverage_before.isoformat() if value.coverage_before else None,
        "last_synced_at": value.last_synced_at.isoformat() if value.last_synced_at else None,
        "reconciliation_state": value.reconciliation_state.value,
        "last_error_code": value.last_error_code,
        **extra,
    }


async def _load_job_context(
    database: Database,
    job_id: uuid.UUID,
    account_id: uuid.UUID,
) -> tuple[ImportJob, ConnectorAccount, GmailCheckpointState]:
    async with database.session_factory() as session:
        job = await session.scalar(
            select(ImportJob).where(ImportJob.id == job_id).with_for_update()
        )
        account = await session.get(ConnectorAccount, account_id)
        if job is None or account is None or job.connector_account_id != account.id:
            raise GmailJobPermanentError("Lavoro Gmail o account non valido")
        if job.source_policy_id is None:
            raise GmailJobPermanentError("Policy Gmail mancante nel lavoro")
        if job.cancel_requested_at is not None:
            job.status = ImportJobStatus.CANCELLED
            job.finished_at = datetime.now(UTC)
            job.checkpoint = {**job.checkpoint, "phase": "cancelled_before_start"}
            await session.commit()
            raise asyncio.CancelledError
        if job.status in {ImportJobStatus.COMPLETED, ImportJobStatus.CANCELLED}:
            raise GmailJobPermanentError("Lavoro Gmail già concluso")
        if account.status != ConnectorAccountStatus.ACTIVE:
            raise GmailJobPermanentError("Account Gmail non attivo")
        if not account.confirmed_aliases:
            raise GmailJobPermanentError("Alias owner Gmail non confermati")
        repository = AsyncPostgresGmailRepository(session)
        checkpoint = await repository.load_checkpoint(account.id, job.source_policy_id)
        job.status = ImportJobStatus.RUNNING
        job.started_at = job.started_at or datetime.now(UTC)
        job.error_code = None
        job.error_detail = None
        job.checkpoint = {**job.checkpoint, "phase": "running"}
        await session.commit()
        return job, account, checkpoint


async def _cancel_requested(database: Database, job_id: uuid.UUID) -> bool:
    async with database.session_factory() as session:
        requested = await session.scalar(
            select(ImportJob.cancel_requested_at).where(ImportJob.id == job_id)
        )
        return requested is not None


async def _update_progress(
    database: Database,
    job_id: uuid.UUID,
    checkpoint: GmailCheckpointState,
    processed: int,
    *,
    phase: str,
    total: int | None = None,
) -> None:
    async with database.session_factory() as session:
        job = await session.get(ImportJob, job_id)
        if job is None:
            return
        job.processed_items = processed
        job.total_items = total
        job.checkpoint = _checkpoint_payload(checkpoint, phase=phase)
        await session.commit()


async def _finish(
    database: Database,
    job_id: uuid.UUID,
    *,
    status: ImportJobStatus,
    checkpoint: GmailCheckpointState | None = None,
    processed: int | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    async with database.session_factory() as session:
        job = await session.get(ImportJob, job_id)
        if job is None:
            return
        job.status = status
        job.finished_at = datetime.now(UTC)
        if processed is not None:
            job.processed_items = processed
        if checkpoint is not None:
            job.checkpoint = _checkpoint_payload(checkpoint, phase=status.value)
        else:
            job.checkpoint = {**job.checkpoint, "phase": status.value}
        job.error_code = error_code
        job.error_detail = error_detail
        await session.commit()


async def _persist_page(
    database: Database,
    account_id: uuid.UUID,
    policy_id: uuid.UUID,
    messages: list[object],
    checkpoint: GmailCheckpointState,
    *,
    removed_ids: frozenset[str] = frozenset(),
    out_of_scope_ids: frozenset[str] = frozenset(),
) -> int:
    from gbrain.domain.gmail import NormalizedGmailMessage

    typed_messages = cast(list[NormalizedGmailMessage], messages)
    async with database.session_factory() as session:
        outcomes = await AsyncPostgresGmailRepository(session).persist_batch(
            account_id,
            policy_id,
            typed_messages,
            checkpoint,
            removed_external_ids=removed_ids,
            out_of_scope_external_ids=out_of_scope_ids,
        )
        return len(outcomes)


async def _run_full(
    database: Database,
    job: ImportJob,
    account: ConnectorAccount,
    connector: GmailSentConnector,
    checkpoint: GmailCheckpointState,
    budget: RequestBudget,
    selection: GmailSelection,
) -> tuple[int, GmailCheckpointState]:
    assert job.source_policy_id is not None
    cursor = GmailCursor(checkpoint.page_token) if checkpoint.page_token else None
    processed = job.processed_items
    current = checkpoint
    while True:
        if await _cancel_requested(database, job.id):
            await _finish(
                database,
                job.id,
                status=ImportJobStatus.CANCELLED,
                checkpoint=current,
                processed=processed,
            )
            raise asyncio.CancelledError
        page = await asyncio.to_thread(connector.enumerate_page, selection, cursor, budget)
        messages: list[object] = []
        out_of_scope: set[str] = set()
        for locator in page.items:
            try:
                raw = await asyncio.to_thread(connector.fetch_item, locator, budget)
                messages.append(connector.normalize_item(raw))
            except ConnectorError as exc:
                if exc.category == ConnectorErrorCategory.PERIMETER:
                    out_of_scope.add(locator.message_id)
                    continue
                raise
        now = datetime.now(UTC)
        current = GmailCheckpointState(
            account_id=str(account.id),
            policy_id=str(job.source_policy_id),
            history_id=(
                cast(Any, messages[-1]).history_id
                if messages and cast(Any, messages[-1]).history_id
                else current.history_id
            ),
            page_token=page.next_cursor.page_token if page.next_cursor else None,
            coverage_after=selection.after,
            coverage_before=selection.before,
            last_synced_at=now,
            reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
            version=current.version,
        )
        processed += await _persist_page(
            database,
            account.id,
            job.source_policy_id,
            messages,
            current,
            out_of_scope_ids=frozenset(out_of_scope),
        )
        await _update_progress(database, job.id, current, processed, phase="page_committed")
        cursor = page.next_cursor
        if cursor is None or page.state != PageState.COMPLETE:
            return processed, current


async def _run_incremental(
    database: Database,
    job: ImportJob,
    account: ConnectorAccount,
    connector: GmailSentConnector,
    checkpoint: GmailCheckpointState,
    budget: RequestBudget,
) -> tuple[int, GmailCheckpointState]:
    assert job.source_policy_id is not None
    if not checkpoint.history_id:
        raise GmailJobPermanentError("L'incrementale richiede un checkpoint history")
    cursor: GmailHistoryCursor | None = GmailHistoryCursor(checkpoint.history_id)
    processed = job.processed_items
    current = checkpoint
    while cursor is not None:
        if await _cancel_requested(database, job.id):
            await _finish(
                database,
                job.id,
                status=ImportJobStatus.CANCELLED,
                checkpoint=current,
                processed=processed,
            )
            raise asyncio.CancelledError
        page = await asyncio.to_thread(connector._gateway.list_history_page, cursor, budget)
        messages: list[object] = []
        out_of_scope: set[str] = set()
        for locator in page.locators:
            try:
                raw = await asyncio.to_thread(connector.fetch_item, locator, budget)
                messages.append(connector.normalize_item(raw))
            except ConnectorError as exc:
                if exc.category == ConnectorErrorCategory.PERIMETER:
                    out_of_scope.add(locator.message_id)
                    continue
                if exc.category == ConnectorErrorCategory.PERMANENT and exc.status_code == 404:
                    continue
                raise
        current = GmailCheckpointState(
            account_id=str(account.id),
            policy_id=str(job.source_policy_id),
            history_id=page.latest_history_id,
            page_token=None,
            coverage_after=checkpoint.coverage_after,
            coverage_before=checkpoint.coverage_before,
            last_synced_at=datetime.now(UTC),
            reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
            version=current.version,
        )
        processed += await _persist_page(
            database,
            account.id,
            job.source_policy_id,
            messages,
            current,
            removed_ids=frozenset(page.deleted_message_ids),
            out_of_scope_ids=frozenset(out_of_scope),
        )
        await _update_progress(database, job.id, current, processed, phase="history_committed")
        cursor = page.next_cursor
    return processed, current


async def _run_reconciliation(
    database: Database,
    job: ImportJob,
    account: ConnectorAccount,
    connector: GmailSentConnector,
    checkpoint: GmailCheckpointState,
    budget: RequestBudget,
    selection: GmailSelection,
) -> tuple[int, GmailCheckpointState]:
    assert job.source_policy_id is not None
    # Una full scan controllata ripristina contenuti/checkpoint; poi marca le assenze
    # senza cancellare record esclusi, come previsto dal repository core.
    async with database.session_factory() as session:
        snapshots = cast(
            tuple[PersistedSnapshot, ...],
            await AsyncPostgresGmailRepository(session).list_snapshots(account.id),
        )
        await session.commit()
    processed, current = await _run_full(
        database, job, account, connector, checkpoint, budget, selection
    )
    observed_ids: set[str] = set()
    cursor: GmailCursor | None = None
    scan_budget = RequestBudget(max_requests=budget.max_requests, max_items=selection.limit)
    while True:
        page = await asyncio.to_thread(connector.enumerate_page, selection, cursor, scan_budget)
        observed_ids.update(locator.message_id for locator in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    missing = frozenset(snapshot.external_id for snapshot in snapshots) - observed_ids
    if missing:
        async with database.session_factory() as session:
            await AsyncPostgresGmailRepository(session).mark_missing(
                account.id, job.source_policy_id, missing, current
            )
    return processed, current


async def _run_preview(
    database: Database,
    job: ImportJob,
    connector: GmailSentConnector,
    budget: RequestBudget,
    selection: GmailSelection,
) -> None:
    result = await asyncio.to_thread(connector.preview_selection, selection, budget)
    # La preview salva solo metriche aggregate nel job: nessun MIME, SourceItem,
    # SourceRevision o checkpoint Gmail viene persistito/avanzato.
    async with database.session_factory() as session:
        current = await session.get(ImportJob, job.id)
        if current is None:
            return
        if current.cancel_requested_at is not None:
            current.status = ImportJobStatus.CANCELLED
            current.finished_at = datetime.now(UTC)
            current.checkpoint = {
                **current.checkpoint,
                "phase": "cancelled",
                "checkpoint_advanced": False,
            }
        else:
            current.status = ImportJobStatus.COMPLETED
            current.processed_items = result.selected_items
            current.total_items = result.selected_items
            current.finished_at = datetime.now(UTC)
            current.checkpoint = {
                "phase": "completed",
                "selected_items": result.selected_items,
                "pages_read": result.pages_read,
                "requests_used": result.requests_used,
                "state": result.state.value,
                "truncated": result.state != PageState.COMPLETE or result.next_cursor_present,
                "persisted_messages": 0,
                "checkpoint_advanced": False,
            }
        await session.commit()


async def execute_gmail_job(
    database: Database,
    settings: Settings,
    *,
    job_id: str,
    account_id: str,
    mode: str,
    gateway_builder: GatewayBuilder = _gateway,
) -> None:
    parsed_job_id = uuid.UUID(job_id)
    parsed_account_id = uuid.UUID(account_id)
    checkpoint: GmailCheckpointState | None = None
    processed = 0
    try:
        job, account, checkpoint = await _load_job_context(
            database, parsed_job_id, parsed_account_id
        )
        credentials = decrypt_credentials(account, settings)
        if not credentials.valid and credentials.refresh_token:
            await asyncio.to_thread(refresh_credentials, credentials, settings)
            account.oauth_token_ciphertext = encrypt_credentials(credentials, settings)
            async with database.session_factory() as session:
                persisted_account = await session.get(ConnectorAccount, account.id)
                if persisted_account is not None:
                    persisted_account.oauth_token_ciphertext = account.oauth_token_ciphertext
                    await session.commit()
        if mode != "preview" and not account.confirmed_aliases:
            raise GmailServiceError(
                "alias_required", "Confermare almeno un alias prima di eseguire l'importazione"
            )
        gateway = await asyncio.to_thread(gateway_builder, account, settings)
        connector = GmailSentConnector(gateway, account.confirmed_aliases)
        selection = _selection(job.policy_snapshot)
        budget = RequestBudget(
            max_requests=int(job.policy_snapshot["request_budget"]),
            max_items=int(job.policy_snapshot["item_limit"]),
        )
        if mode == "preview":
            await _run_preview(database, job, connector, budget, selection)
            return
        if mode == GmailSyncMode.FULL.value:
            processed, checkpoint = await _run_full(
                database, job, account, connector, checkpoint, budget, selection
            )
        elif mode == GmailSyncMode.INCREMENTAL.value:
            processed, checkpoint = await _run_incremental(
                database, job, account, connector, checkpoint, budget
            )
        elif mode == GmailSyncMode.RECONCILIATION.value:
            processed, checkpoint = await _run_reconciliation(
                database, job, account, connector, checkpoint, budget, selection
            )
        else:
            raise GmailJobPermanentError("Modalità lavoro Gmail non valida")
        await _finish(
            database,
            job.id,
            status=ImportJobStatus.COMPLETED,
            checkpoint=checkpoint,
            processed=processed,
        )
    except asyncio.CancelledError:
        return
    except ConnectorError as exc:
        if exc.category == ConnectorErrorCategory.HISTORY_EXPIRED and checkpoint is not None:
            required = checkpoint.require_full_reconciliation("gmail_history_expired")
            await _finish(
                database,
                parsed_job_id,
                status=ImportJobStatus.FAILED,
                checkpoint=required,
                processed=processed,
                error_code="gmail_history_expired",
                error_detail="Cronologia Gmail scaduta: riconciliazione completa richiesta",
            )
            return
        if exc.category in {ConnectorErrorCategory.TRANSIENT, ConnectorErrorCategory.QUOTA}:
            await _finish(
                database,
                parsed_job_id,
                status=ImportJobStatus.FAILED,
                checkpoint=checkpoint,
                processed=processed,
                error_code=f"gmail_{exc.category.value}",
                error_detail=exc.safe_message,
            )
            raise GmailJobTransientError(exc.safe_message) from exc
        await _finish(
            database,
            parsed_job_id,
            status=ImportJobStatus.FAILED,
            checkpoint=checkpoint,
            processed=processed,
            error_code=f"gmail_{exc.category.value}",
            error_detail=exc.safe_message,
        )
    except (GmailOAuthError, GmailServiceError, GmailJobPermanentError) as exc:
        code = getattr(exc, "code", "gmail_job_permanent")
        detail = getattr(exc, "safe_message", str(exc))
        await _finish(
            database,
            parsed_job_id,
            status=ImportJobStatus.FAILED,
            checkpoint=checkpoint,
            processed=processed,
            error_code=str(code),
            error_detail=str(detail)[:500],
        )
