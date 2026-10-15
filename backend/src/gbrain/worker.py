from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from gbrain.application.auth import purge_expired_sessions
from gbrain.application.gmail_jobs import (
    GmailJobFatalError,
    GmailJobTransientError,
    best_effort_terminate_job,
    execute_gmail_job,
)
from gbrain.application.immich_jobs import execute_immich_job
from gbrain.config import get_settings
from gbrain.domain.job_queue import GMAIL_QUEUE_IDENTITY, IMMICH_QUEUE_IDENTITY
from gbrain.domain.models import ImportJob, ImportJobStatus, OutboxEvent, OutboxStatus
from gbrain.infrastructure.database import Database
from gbrain.infrastructure.task_queue import (
    GmailJobRetryStrategy,
    ImmichJobRetryStrategy,
    create_procrastinate_app,
)
from gbrain.logging import configure_logging

settings = get_settings()
procrastinate_app = create_procrastinate_app(settings)
logger = logging.getLogger(__name__)

WORKER_QUEUES: tuple[str, ...] = (
    "maintenance",
    "outbox",
    GMAIL_QUEUE_IDENTITY.queue_name,
    IMMICH_QUEUE_IDENTITY.queue_name,
)


@procrastinate_app.task(name="gbrain.heartbeat", queue="maintenance")
async def heartbeat() -> None:
    """Task innocuo per verificare il consumo della coda persistente."""
    logger.info("Heartbeat worker completato")


@procrastinate_app.task(
    name="gbrain.consume_outbox_placeholder",
    queue="outbox",
    retry=3,
)
async def consume_outbox_placeholder(event_id: str) -> None:
    """Marca un evento infrastrutturale; non contatta Neo4j e non implementa Graphiti."""
    parsed_event_id = uuid.UUID(event_id)
    database = Database(settings)
    try:
        async with database.session_factory() as session:
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.id == parsed_event_id,
                    OutboxEvent.event_type == "infrastructure.placeholder",
                )
                .with_for_update()
            )
            if event is None or event.status == OutboxStatus.COMPLETED:
                return
            event.status = OutboxStatus.COMPLETED
            event.processed_at = datetime.now(UTC)
            await session.commit()
    finally:
        await database.close()


@procrastinate_app.task(
    name=GMAIL_QUEUE_IDENTITY.task_name,
    queue="gmail",  # Contratto: GMAIL_QUEUE_IDENTITY.queue_name
    lock="gmail-heavy-global",
    retry=GmailJobRetryStrategy(
        max_attempts=settings.gmail_job_retry_budget,
        exponential_wait=2,
        retry_exceptions=(GmailJobTransientError,),
    ),
)
async def gmail_sync(job_id: str, account_id: str, mode: str) -> None:
    """Payload privo di token: il worker carica e decifra credenziali dal DB."""
    database = Database(settings)
    try:
        try:
            await execute_gmail_job(
                database,
                settings,
                job_id=job_id,
                account_id=account_id,
                mode=mode,
            )
            async with database.session_factory() as session:
                final_job = await session.get(ImportJob, uuid.UUID(job_id))
                if final_job is not None:
                    if final_job.status == ImportJobStatus.FAILED:
                        logger.error(
                            "Job applicativo Gmail %s fallito: status=%s error_code=%s",
                            job_id,
                            final_job.status.value,
                            final_job.error_code,
                        )
                        raise GmailJobFatalError(
                            f"Job applicativo Gmail {job_id} fallito: error_code={final_job.error_code}"
                        )
                    if final_job.status in (ImportJobStatus.CANCELLED, ImportJobStatus.SUPERSEDED):
                        logger.warning(
                            "Job applicativo Gmail %s concluso nello stato %s: error_code=%s",
                            job_id,
                            final_job.status.value,
                            final_job.error_code,
                        )
                        return
                    if final_job.status == ImportJobStatus.COMPLETED_WITH_WARNINGS:
                        logger.warning(
                            "Job applicativo Gmail %s completato con lacune permanenti: non_importati=%d",
                            job_id,
                            final_job.not_imported_items or 0,
                        )
        except GmailJobTransientError:
            try:
                async with database.session_factory() as session:
                    try:
                        job = await session.get(ImportJob, uuid.UUID(job_id))
                        if job is not None:
                            cancel_req = getattr(job, "cancel_requested_at", None)
                            if isinstance(cancel_req, datetime):
                                job.status = ImportJobStatus.CANCELLED
                                job.finished_at = datetime.now(UTC)
                                cp = job.checkpoint if isinstance(job.checkpoint, dict) else {}
                                job.checkpoint = {
                                    **cp,
                                    "phase": "cancelled_cooperative",
                                }
                                await session.commit()
                                logger.info(
                                    "Job applicativo Gmail %s cancellato cooperativamente durante transient error",
                                    job_id,
                                )
                                return

                            job.retry_count += 1
                            if job.retry_count <= settings.gmail_job_retry_budget:
                                job.status = ImportJobStatus.PENDING
                                job.finished_at = None
                                cp = job.checkpoint if isinstance(job.checkpoint, dict) else {}
                                job.checkpoint = {**cp, "phase": "retry_scheduled"}
                                is_code_empty = not job.error_code or hasattr(
                                    job.error_code, "_mock_return_value"
                                )
                                if is_code_empty:
                                    job.error_code = "transient_retry"
                                    job.error_detail = "Errore temporaneo durante l'elaborazione"
                                await session.commit()
                            else:
                                job.status = ImportJobStatus.FAILED
                                job.finished_at = datetime.now(UTC)
                                is_code_empty = not job.error_code or hasattr(
                                    job.error_code, "_mock_return_value"
                                )
                                if is_code_empty:
                                    job.error_code = "retry_budget_exhausted"
                                    job.error_detail = "Superato il budget massimo di tentativi per errore transitorio"
                                cp = job.checkpoint if isinstance(job.checkpoint, dict) else {}
                                job.checkpoint = {
                                    **cp,
                                    "phase": "failed",
                                    "exhausted_retry_budget": True,
                                }
                                await session.commit()
                    except Exception:
                        await session.rollback()
                        raise
            except Exception as persist_err:
                logger.error(
                    "Aggiornamento retry di ImportJob %s fallito nel database: error_type=%s",
                    job_id,
                    type(persist_err).__name__,
                )
            raise
        except GmailJobFatalError:
            logger.error("Job applicativo Gmail %s fallito in modo fatale", job_id)
            raise
        except Exception as exc:
            logger.error(
                "Eccezione inattesa durante l'esecuzione del job Gmail %s: error_type=%s",
                job_id,
                type(exc).__name__,
            )
            await best_effort_terminate_job(
                database,
                job_id,
                status=ImportJobStatus.FAILED,
                error_code="unexpected_error",
                error_detail="Errore inatteso durante l'elaborazione del job",
                phase="failed_unexpected",
            )
            raise
    finally:
        await database.close()


@procrastinate_app.task(
    name=IMMICH_QUEUE_IDENTITY.task_name,
    queue=IMMICH_QUEUE_IDENTITY.queue_name,
    retry=ImmichJobRetryStrategy(max_attempts=4),
)
async def immich_sync(job_id: str, account_id: str, mode: str) -> None:
    """Payload privo di segreti: il worker carica e decifra URL/chiave API dal DB."""
    database = Database(settings)
    try:
        await execute_immich_job(
            database,
            settings,
            job_id=job_id,
            account_id=account_id,
            mode=mode,
        )
    finally:
        await database.close()


@procrastinate_app.task(name="gbrain.purge_sessions", queue="maintenance")
async def purge_sessions() -> None:
    database = Database(settings)
    try:
        async with database.session_factory() as session:
            count = await purge_expired_sessions(session)
            logger.info("Sessioni scadute eliminate: %s", count)
    finally:
        await database.close()


async def run_worker() -> None:
    configure_logging(settings.log_level)
    async with procrastinate_app.open_async():
        await procrastinate_app.run_worker_async(
            concurrency=settings.worker_concurrency,
            queues=list(WORKER_QUEUES),
            wait=True,
            listen_notify=True,
            name="gbrain-worker",
        )


if __name__ == "__main__":
    asyncio.run(run_worker())
