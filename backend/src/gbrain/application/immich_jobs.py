from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from gbrain.config import Settings
from gbrain.domain.immich import (
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


async def execute_immich_job(
    database: Database,
    settings: Settings,
    *,
    job_id: str,
    account_id: str,
    mode: str,
    gateway_factory: Any | None = None,
) -> None:
    """Esecutore asincrono del lavoro di importazione Immich (read-only, max 25 asset).

    Nessuna credenziale in chiaro viaggia nel payload del task; l'URL e la chiave API
    vengono caricati e decifrati esclusivamente a runtime dal DB PostgreSQL protetto con AES-256-GCM.
    """
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

        # Recupera e decifra credenziali AES-256-GCM
        cred_repo = SourceCredentialsRepository()
        raw_payload = await cred_repo.get_decrypted_payload(
            session,
            settings.source_credentials_encryption_key,
            account.owner_id,
            IMMICH_SOURCE_KIND,
        )
        if not raw_payload:
            job.status = ImportJobStatus.FAILED
            job.error_code = "credentials_missing"
            job.error_detail = "Credenziali Immich non trovate o non decifrabili"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        base_url = raw_payload.get("base_url")
        api_key = raw_payload.get("api_key")
        if not base_url or not api_key:
            job.status = ImportJobStatus.FAILED
            job.error_code = "credentials_invalid"
            job.error_detail = "Configurazione credenziali Immich incompleta"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        # Marca job in stato running
        job.status = ImportJobStatus.RUNNING
        job.started_at = job.started_at or datetime.now(UTC)
        await session.commit()

    if gateway_factory is not None:
        gateway = gateway_factory(base_url, api_key)
    else:
        gateway = ImmichGateway(base_url, api_key)

    snapshot = job.policy_snapshot or {}
    try:
        album_id = snapshot.get("album_id")
        raw_after = snapshot.get("after")
        raw_before = snapshot.get("before")
        after = datetime.fromisoformat(str(raw_after)) if raw_after else None
        before = datetime.fromisoformat(str(raw_before)) if raw_before else None
        limit = min(int(snapshot.get("item_limit", IMMICH_MAX_ITEMS_LIMIT)), IMMICH_MAX_ITEMS_LIMIT)

        selection = ImmichSelection(
            album_id=album_id,
            after=after,
            before=before,
            item_limit=limit,
        )
    except Exception as exc:
        logger.error("Snapshot selezione Immich non valido per job %s: %s", job_id, exc)
        async with database.session_factory() as session:
            j = await session.get(ImportJob, parsed_job_id)
            if j and j.status not in _TERMINAL_JOB_STATUSES:
                j.status = ImportJobStatus.FAILED
                j.error_code = "invalid_selection_snapshot"
                j.error_detail = "Parametri di selezione Immich non validi"
                j.finished_at = datetime.now(UTC)
                await session.commit()
        return

    try:
        # Enumerazione read-only degli asset (max 25)
        if selection.album_id:
            _, assets = await gateway.fetch_album_assets(
                selection.album_id, limit=selection.item_limit
            )
        else:
            assets = await gateway.search_period_assets(
                selection.after, selection.before, limit=selection.item_limit
            )

        # Elaborazione ed upsert atomico batch
        async with database.session_factory() as session:
            repo = AsyncPostgresImmichRepository(session)
            checkpoint = {
                "phase": "completed",
                "total_enumerated": len(assets),
                "item_limit": selection.item_limit,
                "album_id": selection.album_id,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            inserted, duplicate = await repo.persist_asset_batch(
                parsed_account_id,
                assets,
                job_id=parsed_job_id,
                checkpoint=checkpoint,
            )

            # Finalizza job
            j = await session.get(ImportJob, parsed_job_id)
            if j and j.status not in _TERMINAL_JOB_STATUSES:
                j.status = ImportJobStatus.COMPLETED
                j.finished_at = datetime.now(UTC)
                await session.commit()

        logger.info(
            "Job Immich %s completato con successo: inseriti=%d duplicati=%d totali=%d",
            job_id,
            inserted,
            duplicate,
            len(assets),
        )

    except ImmichGatewayError as gw_err:
        logger.warning(
            "Errore gateway Immich durante job %s: code=%s retryable=%s",
            job_id,
            gw_err.reason_code,
            gw_err.retryable,
        )
        if gw_err.retryable:
            # Aggiorna checkpoint con errore transitorio per il prossimo retry
            async with database.session_factory() as session:
                j = await session.get(ImportJob, parsed_job_id)
                if j and j.status not in _TERMINAL_JOB_STATUSES:
                    j.error_code = gw_err.reason_code
                    j.error_detail = gw_err.message
                    await session.commit()
            raise
        else:
            # Errore permanente (es. auth revocata o endpoint mancante)
            async with database.session_factory() as session:
                j = await session.get(ImportJob, parsed_job_id)
                if j and j.status not in _TERMINAL_JOB_STATUSES:
                    j.status = ImportJobStatus.FAILED
                    j.error_code = gw_err.reason_code
                    j.error_detail = gw_err.message
                    j.finished_at = datetime.now(UTC)
                    await session.commit()
    except Exception:
        logger.exception("Eccezione imprevista durante esecuzione job Immich %s", job_id)
        async with database.session_factory() as session:
            j = await session.get(ImportJob, parsed_job_id)
            if j and j.status not in _TERMINAL_JOB_STATUSES:
                j.status = ImportJobStatus.FAILED
                j.error_code = "unexpected_error"
                j.error_detail = "Errore imprevisto durante l'importazione Immich"
                j.finished_at = datetime.now(UTC)
                await session.commit()
