from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import procrastinate
from sqlalchemy import select

from gbrain.application.auth import purge_expired_sessions
from gbrain.application.gmail_jobs import GmailJobTransientError, execute_gmail_job
from gbrain.config import get_settings
from gbrain.domain.models import ImportJob, ImportJobStatus, OutboxEvent, OutboxStatus
from gbrain.infrastructure.database import Database
from gbrain.infrastructure.task_queue import GMAIL_TASK_NAME, create_procrastinate_app
from gbrain.logging import configure_logging

settings = get_settings()
procrastinate_app = create_procrastinate_app(settings)
logger = logging.getLogger(__name__)


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
    name=GMAIL_TASK_NAME,
    queue="gmail",
    lock="gmail-heavy-global",
    retry=procrastinate.RetryStrategy(
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
        except GmailJobTransientError:
            async with database.session_factory() as session:
                job = await session.get(ImportJob, uuid.UUID(job_id))
                if job is not None:
                    job.retry_count += 1
                    if job.retry_count <= settings.gmail_job_retry_budget:
                        # Il job deve tornare eseguibile prima che Procrastinate
                        # programmi il tentativo successivo consentito.
                        job.status = ImportJobStatus.PENDING
                        job.finished_at = None
                        job.checkpoint = {**job.checkpoint, "phase": "retry_scheduled"}
                    await session.commit()
            raise
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
            queues=["maintenance", "outbox"],
            wait=True,
            listen_notify=True,
            name="gbrain-worker",
        )


if __name__ == "__main__":
    asyncio.run(run_worker())
