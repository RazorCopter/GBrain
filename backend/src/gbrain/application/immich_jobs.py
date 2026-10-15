from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from gbrain.config import Settings
from gbrain.domain.immich import (
    IMMICH_API_BATCH_SIZE,
    IMMICH_DEFAULT_ITEMS_LIMIT,
    IMMICH_DEFAULT_REQUEST_BUDGET,
    IMMICH_MAX_ITEMS_LIMIT,
    IMMICH_SOURCE_KIND,
    ImmichGatewayError,
    ImmichSelection,
)
from gbrain.domain.models import (
    ConnectorAccount,
    ConnectorAccountStatus,
    ImportJob,
    ImportJobStatus,
)
from gbrain.infrastructure.database import Database
from gbrain.infrastructure.immich_gateway import ImmichGateway
from gbrain.infrastructure.immich_repository import AsyncPostgresImmichRepository
from gbrain.infrastructure.source_credentials_repository import SourceCredentialsRepository

logger = logging.getLogger(__name__)

_TERMINAL_JOB_STATUSES = {
    ImportJobStatus.COMPLETED,
    ImportJobStatus.COMPLETED_WITH_WARNINGS,
    ImportJobStatus.FAILED,
    ImportJobStatus.CANCELLED,
    ImportJobStatus.SUPERSEDED,
}


def _checkpoint_int(checkpoint: dict[str, Any], key: str) -> int:
    value = checkpoint.get(key, 0)
    return value if isinstance(value, int) and value >= 0 else 0


async def _cancel_if_requested(database: Database, job_id: uuid.UUID) -> bool:
    """Controlla il contratto condiviso di cancellazione fra due batch."""
    async with database.session_factory() as session:
        job = await session.get(ImportJob, job_id)
        if job is None or job.cancel_requested_at is None:
            return False
        if job.status not in _TERMINAL_JOB_STATUSES:
            checkpoint = job.checkpoint if isinstance(job.checkpoint, dict) else {}
            job.status = ImportJobStatus.CANCELLED
            job.finished_at = datetime.now(UTC)
            job.checkpoint = {**checkpoint, "phase": "cancelled", "stopped_reason": "cancelled"}
            await session.commit()
        return True


async def execute_immich_job(
    database: Database,
    settings: Settings,
    *,
    job_id: str,
    account_id: str,
    mode: str,
    gateway_factory: Any | None = None,
) -> None:
    """Importa metadati Immich read-only in batch atomici e riprendibili da 25 asset."""
    parsed_job_id = uuid.UUID(job_id)
    parsed_account_id = uuid.UUID(account_id)

    async with database.session_factory() as session:
        job = await session.get(ImportJob, parsed_job_id)
        account = await session.get(ConnectorAccount, parsed_account_id)

        if job is None or account is None or job.connector_account_id != account.id:
            logger.error("Job Immich %s o account %s non trovato", job_id, account_id)
            return
        if job.status in _TERMINAL_JOB_STATUSES:
            logger.info("Job Immich %s già concluso (status=%s)", job_id, job.status)
            return
        if account.status != ConnectorAccountStatus.ACTIVE:
            job.status = ImportJobStatus.FAILED
            job.error_code = "account_not_active"
            job.error_detail = "Account Immich non attivo o autorizzazione revocata"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        cred_repo = SourceCredentialsRepository()
        raw_payload = await cred_repo.get_decrypted_payload(
            session,
            settings.source_credentials_encryption_key,
            owner_id=account.owner_id,
            source_kind=IMMICH_SOURCE_KIND,
        )
        if raw_payload is None:
            job.status = ImportJobStatus.FAILED
            job.error_code = "credentials_not_found"
            job.error_detail = "Credenziali Immich non configurate"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        base_url = raw_payload.get("base_url")
        api_key = raw_payload.get("api_key")
        if not isinstance(base_url, str) or not isinstance(api_key, str):
            job.status = ImportJobStatus.FAILED
            job.error_code = "credentials_invalid"
            job.error_detail = "Credenziali Immich non valide"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        snapshot = job.policy_snapshot if isinstance(job.policy_snapshot, dict) else {}
        checkpoint = job.checkpoint if isinstance(job.checkpoint, dict) else {}
        job.status = ImportJobStatus.RUNNING
        job.started_at = job.started_at or datetime.now(UTC)
        job.finished_at = None
        job.error_code = None
        job.error_detail = None
        await session.commit()

    try:
        album_id_raw = snapshot.get("album_id")
        album_id = str(album_id_raw) if album_id_raw else None
        raw_after = snapshot.get("after")
        raw_before = snapshot.get("before")
        after = datetime.fromisoformat(str(raw_after)) if raw_after else None
        before = datetime.fromisoformat(str(raw_before)) if raw_before else None
        import_all = bool(snapshot.get("import_all", False))
        item_limit = int(snapshot.get("item_limit", IMMICH_DEFAULT_ITEMS_LIMIT))
        request_budget = int(snapshot.get("request_budget", IMMICH_DEFAULT_REQUEST_BUDGET))
        if request_budget < 1:
            raise ValueError("request_budget deve essere positivo")
        selection = ImmichSelection(
            album_id=album_id,
            after=after,
            before=before,
            import_all=import_all,
            item_limit=min(item_limit, IMMICH_MAX_ITEMS_LIMIT),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        logger.warning("Snapshot selezione Immich non valido per job %s: %s", job_id, exc)
        async with database.session_factory() as session:
            job = await session.get(ImportJob, parsed_job_id)
            if job is not None and job.status not in _TERMINAL_JOB_STATUSES:
                job.status = ImportJobStatus.FAILED
                job.error_code = "invalid_selection_snapshot"
                job.error_detail = "Parametri di selezione Immich non validi"
                job.finished_at = datetime.now(UTC)
                await session.commit()
        return

    gateway = (
        gateway_factory(base_url, api_key)
        if gateway_factory is not None
        else ImmichGateway(base_url, api_key)
    )

    total_enumerated = _checkpoint_int(checkpoint, "total_enumerated")
    total_inserted = _checkpoint_int(checkpoint, "total_inserted")
    total_duplicates = _checkpoint_int(checkpoint, "total_duplicates")
    pages_completed = _checkpoint_int(checkpoint, "pages_completed")
    requests_completed = _checkpoint_int(checkpoint, "requests_completed")
    cursor_raw = checkpoint.get("cursor")
    cursor = cursor_raw if isinstance(cursor_raw, str) and cursor_raw else None
    album_offset = _checkpoint_int(checkpoint, "album_offset")
    album_assets: list[Any] | None = None
    stopped_reason: str | None = None

    try:
        while True:
            if await _cancel_if_requested(database, parsed_job_id):
                return
            if total_enumerated >= selection.item_limit:
                stopped_reason = "item_limit_reached"
                break

            remaining = selection.item_limit - total_enumerated
            batch_limit = min(IMMICH_API_BATCH_SIZE, remaining)
            if selection.album_id is not None:
                if album_assets is None:
                    if requests_completed >= request_budget:
                        stopped_reason = "request_budget_reached"
                        break
                    _, album_assets = await gateway.fetch_album_assets(selection.album_id)
                    requests_completed += 1
                assets = album_assets[album_offset : album_offset + batch_limit]
                next_album_offset = album_offset + len(assets)
                has_more = next_album_offset < len(album_assets)
                next_cursor = None
            else:
                if requests_completed >= request_budget:
                    stopped_reason = "request_budget_reached"
                    break
                assets, next_cursor = await gateway.search_period_assets(
                    selection.after,
                    selection.before,
                    batch_limit,
                    cursor=cursor,
                )
                requests_completed += 1
                next_album_offset = album_offset
                has_more = next_cursor is not None

            next_enumerated = total_enumerated + len(assets)
            next_pages = pages_completed + 1
            next_checkpoint: dict[str, Any] = {
                "phase": "paginating",
                "cursor": next_cursor,
                "album_offset": next_album_offset,
                "total_enumerated": next_enumerated,
                "total_inserted": total_inserted,
                "total_duplicates": total_duplicates,
                "pages_completed": next_pages,
                "requests_completed": requests_completed,
                "item_limit": selection.item_limit,
                "request_budget": request_budget,
                "batch_size": IMMICH_API_BATCH_SIZE,
            }

            async with database.session_factory() as session:
                repo = AsyncPostgresImmichRepository(session)
                inserted, duplicates = await repo.persist_asset_batch(
                    parsed_account_id,
                    assets,
                    job_id=parsed_job_id,
                    checkpoint=next_checkpoint,
                )

            total_enumerated = next_enumerated
            total_inserted += inserted
            total_duplicates += duplicates
            pages_completed = next_pages
            cursor = next_cursor
            album_offset = next_album_offset

            # I contatori vengono riallineati dal checkpoint al riavvio; asset e posizione
            # di resume sono già stati committati atomicamente dal repository.
            async with database.session_factory() as session:
                job = await session.get(ImportJob, parsed_job_id)
                if job is not None:
                    current = job.checkpoint if isinstance(job.checkpoint, dict) else {}
                    job.checkpoint = {
                        **current,
                        "total_inserted": total_inserted,
                        "total_duplicates": total_duplicates,
                    }
                    job.processed_items = total_enumerated
                    job.imported_items = total_inserted
                    job.duplicate_items = total_duplicates
                    await session.commit()

            if not has_more:
                stopped_reason = "source_exhausted"
                break

        incomplete = stopped_reason in {"item_limit_reached", "request_budget_reached"}
        async with database.session_factory() as session:
            job = await session.get(ImportJob, parsed_job_id)
            if job is None or job.status in _TERMINAL_JOB_STATUSES:
                return
            current = job.checkpoint if isinstance(job.checkpoint, dict) else {}
            job.status = (
                ImportJobStatus.COMPLETED_WITH_WARNINGS if incomplete else ImportJobStatus.COMPLETED
            )
            job.processed_items = total_enumerated
            job.imported_items = total_inserted
            job.duplicate_items = total_duplicates
            job.total_items = total_enumerated
            job.finished_at = datetime.now(UTC)
            job.checkpoint = {
                **current,
                "phase": "incomplete" if incomplete else "completed",
                "cursor": cursor,
                "album_offset": album_offset,
                "total_enumerated": total_enumerated,
                "total_inserted": total_inserted,
                "total_duplicates": total_duplicates,
                "pages_completed": pages_completed,
                "requests_completed": requests_completed,
                "stopped_reason": stopped_reason,
            }
            if incomplete:
                job.error_code = stopped_reason
                job.error_detail = (
                    "Importazione parziale: aumentare il limite totale o il budget richieste"
                )
            await session.commit()
    except ImmichGatewayError as exc:
        logger.warning(
            "Job Immich %s interrotto: category=%s code=%s",
            job_id,
            exc.category.value,
            exc.reason_code,
        )
        if exc.retryable:
            raise
        async with database.session_factory() as session:
            job = await session.get(ImportJob, parsed_job_id)
            if job is not None and job.status not in _TERMINAL_JOB_STATUSES:
                current = job.checkpoint if isinstance(job.checkpoint, dict) else {}
                job.status = ImportJobStatus.FAILED
                job.error_code = exc.reason_code
                job.error_detail = exc.message
                job.finished_at = datetime.now(UTC)
                job.checkpoint = {**current, "phase": "failed"}
                await session.commit()
