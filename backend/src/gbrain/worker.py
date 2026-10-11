from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import procrastinate
from sqlalchemy import select

from gbrain.application.auth import purge_expired_sessions
from gbrain.config import get_settings
from gbrain.domain.models import OutboxEvent, OutboxStatus
from gbrain.infrastructure.database import Database
from gbrain.logging import configure_logging

settings = get_settings()
connector = procrastinate.PsycopgConnector(conninfo=settings.procrastinate_dsn)
procrastinate_app = procrastinate.App(connector=connector)
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
    await procrastinate_app.run_worker_async(
        concurrency=settings.worker_concurrency,
        queues=["maintenance", "outbox"],
        wait=True,
        listen_notify=True,
        name="gbrain-worker",
    )


if __name__ == "__main__":
    asyncio.run(run_worker())
