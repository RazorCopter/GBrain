from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gbrain.application.import_job_runtime import (
    ImportJobConsistency,
    ImportJobRuntimeStatus,
    derive_import_job_runtime,
    is_queue_identity_coherent,
)
from gbrain.domain.models import (
    AuthSession,
    Base,
    ConnectorAccount,
    ConnectorAccountStatus,
    ImportJob,
    ImportJobStatus,
    OwnerAccount,
)
from gbrain.infrastructure.procrastinate_jobs import fetch_procrastinate_job_snapshot
from gbrain.infrastructure.task_queue import GMAIL_QUEUE_NAME, GMAIL_TASK_NAME
from gbrain.presentation.dependencies import Principal
from gbrain.presentation.gmail_api import cancel_job_endpoint

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Connessione PostgreSQL locale per test di integrazione reale (schema Procrastinate 3.10)
PG_HOST = os.getenv("TEST_PG_HOST", "localhost")
PG_PORT = os.getenv("TEST_PG_PORT", "5433")
PG_USER = os.getenv("TEST_PG_USER", "gbrain")
PG_PASSWORD = os.getenv("TEST_PG_PASSWORD", "test-secret")
PG_DB = os.getenv("TEST_PG_DB", "gbrain_test")

TEST_DATABASE_URL = f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"


def _can_connect_postgres() -> bool:
    try:
        with psycopg.connect(
            host=PG_HOST,
            port=int(PG_PORT),
            user=PG_USER,
            password=PG_PASSWORD,
            dbname=PG_DB,
            connect_timeout=2,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _can_connect_postgres(),
    reason="PostgreSQL locale reale (Docker gbrain-pg-test su 5433) non disponibile",
)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def pg_engine() -> Any:
    # Applica schema Procrastinate 3.10 nativo nel database PostgreSQL locale reale
    sql = files("procrastinate").joinpath("sql/schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(
        host=PG_HOST,
        port=int(PG_PORT),
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DB,
        autocommit=True,
    ) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
        conn.execute(sql)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    # Crea anche le tabelle del modello applicativo gbrain (OwnerAccount, ConnectorAccount, ImportJob, etc.)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest.fixture
async def pg_session(pg_engine: Any) -> Any:
    maker = async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.mark.asyncio
async def test_procrastinate_3_10_real_query_snapshot(pg_session: AsyncSession) -> None:
    """Verifica che la query reale di snapshot legga fedelmente lo schema Procrastinate 3.10 reale,

    inclusi il clock PostgreSQL `clock_timestamp()`, il parsing JSON degli args, l'evento
    `deferred_for_retry` nella tabella `procrastinate_events`, e la rilevazione del blocco lock.
    """
    app_job_id = uuid.uuid4()
    lock_key = f"gmail-lock-{uuid.uuid4()}"

    # 1. Inserisci un job blocker con status 'doing' per lo stesso lock
    insert_blocker = sa.text(
        """
        INSERT INTO procrastinate_jobs (
            queue_name, task_name, lock, queueing_lock, status,
            scheduled_at, attempts, priority, args
        ) VALUES (
            :queue_name, :task_name, :lock, NULL, 'doing',
            clock_timestamp(),
            1, 0, '{"blocker": true}'::jsonb
        ) RETURNING id;
        """
    )
    res_blocker = await pg_session.execute(
        insert_blocker,
        {
            "queue_name": GMAIL_QUEUE_NAME,
            "task_name": GMAIL_TASK_NAME,
            "lock": lock_key,
        },
    )
    blocker_id = res_blocker.scalar_one()
    assert blocker_id > 0

    # 2. Inserisci job target (status = todo, scheduled_at tra 120 secondi nel futuro rispetto al DB clock)
    insert_target = sa.text(
        """
        INSERT INTO procrastinate_jobs (
            queue_name, task_name, lock, queueing_lock, status,
            scheduled_at, attempts, priority, args
        ) VALUES (
            :queue_name, :task_name, :lock, NULL, 'todo',
            clock_timestamp() + interval '120 seconds',
            1, 0, CAST(:args AS jsonb)
        ) RETURNING id;
        """
    )
    res_target = await pg_session.execute(
        insert_target,
        {
            "queue_name": GMAIL_QUEUE_NAME,
            "task_name": GMAIL_TASK_NAME,
            "lock": lock_key,
            "args": f'{{"job_id": "{app_job_id}", "mode": "full"}}',
        },
    )
    target_id = res_target.scalar_one()

    # 3. Inserisci evento di retry per il job target
    insert_event = sa.text(
        """
        INSERT INTO procrastinate_events (job_id, type, at)
        VALUES (:job_id, 'deferred_for_retry', clock_timestamp());
        """
    )
    await pg_session.execute(insert_event, {"job_id": target_id})
    await pg_session.commit()

    # 4. Esegui la query infrastrutturale reale dell'applicazione
    snapshot = await fetch_procrastinate_job_snapshot(pg_session, target_id)
    assert snapshot is not None

    # Verifiche rigorose di integrazione reale:
    assert snapshot.job_id == target_id
    assert snapshot.queue_name == GMAIL_QUEUE_NAME
    assert snapshot.task_name == GMAIL_TASK_NAME
    assert snapshot.lock == lock_key
    assert snapshot.status == "todo"
    assert snapshot.attempts == 1
    assert snapshot.abort_requested is False
    assert snapshot.has_retry_event is True
    # blocked_by_lock DEVE essere True a causa del job blocker nello stato 'doing' con lo stesso lock
    assert snapshot.blocked_by_lock is True
    assert snapshot.args == {"job_id": str(app_job_id), "mode": "full"}

    # Clock PostgreSQL reale: database_now deve essere coerente con il tempo reale UTC del server PG
    assert snapshot.database_now.tzinfo is not None
    assert snapshot.scheduled_at is not None
    remaining = (snapshot.scheduled_at - snapshot.database_now).total_seconds()
    assert 115.0 <= remaining <= 125.0

    # 5. Verifica derivazione del runtime model
    dummy_job = ImportJob(
        id=app_job_id,
        source_kind="gmail",
        connector_account_id=uuid.uuid4(),
        queue_job_id=target_id,
        status=ImportJobStatus.PENDING,
        mode="full",
        checkpoint={"phase": "running"},
        policy_snapshot={},
        processed_items=0,
        retry_count=1,
        created_at=datetime.now(UTC),
    )
    assert is_queue_identity_coherent(dummy_job, snapshot) is True

    runtime = derive_import_job_runtime(dummy_job, snapshot)
    assert runtime.status == ImportJobRuntimeStatus.SCHEDULED_RETRY
    assert runtime.consistency == ImportJobConsistency.CONSISTENT
    assert runtime.retry is not None
    assert runtime.retry.attempts == 1
    assert 115.0 <= runtime.retry.remaining_seconds <= 125.0


@pytest.mark.asyncio
async def test_procrastinate_3_10_real_cancellation_and_idempotence(
    pg_session: AsyncSession,
) -> None:
    """Verifica il comportamento reale di cancellazione su PostgreSQL con Schema Procrastinate 3.10 reale."""
    app_job_id = uuid.uuid4()

    # Inserisci job nello stato 'todo'
    insert_stmt = sa.text(
        """
        INSERT INTO procrastinate_jobs (
            queue_name, task_name, lock, queueing_lock, status,
            scheduled_at, attempts, priority, args
        ) VALUES (
            :queue_name, :task_name, 'gmail-heavy-global', NULL, 'todo',
            clock_timestamp(), 0, 0, CAST(:args AS jsonb)
        ) RETURNING id;
        """
    )
    res = await pg_session.execute(
        insert_stmt,
        {
            "queue_name": GMAIL_QUEUE_NAME,
            "task_name": GMAIL_TASK_NAME,
            "args": f'{{"job_id": "{app_job_id}", "mode": "full"}}',
        },
    )
    queue_job_id = res.scalar_one()
    await pg_session.commit()

    # 1. Esegui la funzione nativa di cancellazione di Procrastinate 3.10 (procrastinate_cancel_job_v1)
    cancel_fn_stmt = sa.text("SELECT procrastinate_cancel_job_v1(:target_id, false, false);")
    res_cancel_1 = await pg_session.execute(cancel_fn_stmt, {"target_id": queue_job_id})
    cancelled_first = res_cancel_1.scalar_one()
    await pg_session.commit()
    assert bool(cancelled_first) is True

    # 2. Leggi lo snapshot reale dal DB dopo la prima cancellazione tramite fetch_procrastinate_job_snapshot
    snapshot_after = await fetch_procrastinate_job_snapshot(pg_session, queue_job_id)
    assert snapshot_after is not None
    assert snapshot_after.status == "cancelled"

    # 3. Riprova la cancellazione (seconda invocazione della saga / retry per failure parziale)
    # La procedura PostgreSQL nativa di Procrastinate restituisce 0 (False) perché lo status non è più 'todo'
    res_cancel_2 = await pg_session.execute(cancel_fn_stmt, {"target_id": queue_job_id})
    cancelled_second = res_cancel_2.scalar_one()
    await pg_session.commit()
    assert bool(cancelled_second) is False

    # La nostra logica applicativa osserva lo stato reale 'cancelled' e converge all'idempotenza
    recheck_snapshot = await fetch_procrastinate_job_snapshot(pg_session, queue_job_id)
    assert recheck_snapshot is not None
    assert recheck_snapshot.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_job_endpoint_saga_real_postgres(
    pg_session: AsyncSession,
) -> None:
    """Attraversa la saga applicativa cancel_job_endpoint con DB app + schema Procrastinate su PostgreSQL reale.

    Verifica:
    1. Creazione record applicativi (OwnerAccount, ConnectorAccount, ImportJob) e job Procrastinate reale.
    2. Marker-first: marcatura applicativa cancel_requested_at salvata sul DB prima dell'I/O queue.
    3. Failure intermedia controllata simulata durante l'operazione sulla coda.
    4. Riconciliazione successiva: il job in coda viene cancellato, una seconda invocazione riconcilia
       l'ImportJob su CANCELLED (phase: cancelled_reconciled) leggendo lo snapshot da PostgreSQL reale.
    5. Idempotenza terminale: ulteriori chiamate restituiscono esito immutato senza errori.
    """
    app_job_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    connector_id = uuid.uuid4()

    # 1. Inserisci record applicativi su PostgreSQL reale
    owner = OwnerAccount(
        id=owner_id,
        owner_slot=1,
        username=f"saga_owner_{uuid.uuid4().hex[:8]}",
        display_name="Saga Owner",
        password_hash="test-hash",  # noqa: S106
        is_active=True,
    )
    pg_session.add(owner)
    await pg_session.flush()

    auth_session = AuthSession(
        id=uuid.uuid4(),
        owner_id=owner.id,
        token_hash=b"0" * 32,
        csrf_hash=b"1" * 32,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    pg_session.add(auth_session)
    await pg_session.flush()

    connector = ConnectorAccount(
        id=connector_id,
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id=f"saga_{uuid.uuid4().hex[:8]}@example.com",
        primary_email=f"saga_{uuid.uuid4().hex[:8]}@example.com",
        status=ConnectorAccountStatus.ACTIVE,
    )
    pg_session.add(connector)
    await pg_session.flush()

    # Inserisci il job Procrastinate 3.10 reale in stato 'todo'
    insert_job_stmt = sa.text(
        """
        INSERT INTO procrastinate_jobs (
            queue_name, task_name, lock, queueing_lock, status,
            scheduled_at, attempts, priority, args
        ) VALUES (
            :queue_name, :task_name, 'gmail-heavy-global', NULL, 'todo',
            clock_timestamp(), 0, 0, CAST(:args AS jsonb)
        ) RETURNING id;
        """
    )
    res_queue = await pg_session.execute(
        insert_job_stmt,
        {
            "queue_name": GMAIL_QUEUE_NAME,
            "task_name": GMAIL_TASK_NAME,
            "args": f'{{"job_id": "{app_job_id}", "mode": "full"}}',
        },
    )
    queue_job_id = res_queue.scalar_one()

    # Crea l'ImportJob applicativo associato a queue_job_id
    app_job = ImportJob(
        id=app_job_id,
        source_kind="gmail",
        connector_account_id=connector.id,
        queue_job_id=queue_job_id,
        mode="full",
        status=ImportJobStatus.PENDING,
        checkpoint={"phase": "running"},
        policy_snapshot={},
        processed_items=0,
        retry_count=0,
    )
    pg_session.add(app_job)
    await pg_session.commit()

    # 2. Configura queue con failure intermedia controllata (es. timeout o errore di rete)
    mock_queue = MagicMock()
    mock_queue.job_manager.cancel_job_by_id_async = AsyncMock(
        side_effect=RuntimeError("Simulazione transitoria di fallimento della chiamata alla coda")
    )
    principal = Principal(owner=owner, session=auth_session)

    # Invocazione 1: attraversa la saga e incontra la failure intermedia
    resp_first = await cancel_job_endpoint(
        job_id=app_job.id,
        session=pg_session,
        queue=mock_queue,
        principal=principal,
    )

    # Verifica Marker-First persistito durevolmente su PostgreSQL reale:
    # cancel_requested_at DEVE essere valorizzato nel DB, e il checkpoint deve riflettere la richiesta
    await pg_session.refresh(app_job)
    assert app_job.cancel_requested_at is not None
    assert app_job.checkpoint["phase"] == "cancellation_requested"
    # Lo stato applicativo resta PENDING (non ancora terminato a causa del failure intermedio sulla queue)
    assert app_job.status == ImportJobStatus.PENDING
    assert resp_first.status == "pending"

    # 3. Il job nella coda Procrastinate reale converge a 'cancelled' (tramite procedura SQL nativa Procrastinate)
    cancel_fn_stmt = sa.text("SELECT procrastinate_cancel_job_v1(:target_id, false, false);")
    res_cancel = await pg_session.execute(cancel_fn_stmt, {"target_id": queue_job_id})
    assert bool(res_cancel.scalar_one()) is True
    await pg_session.commit()

    # 4. Invocazione 2: riconciliazione successiva della saga
    # Legge lo snapshot reale da PostgreSQL (stato 'cancelled') e porta l'ImportJob su CANCELLED
    resp_second = await cancel_job_endpoint(
        job_id=app_job.id,
        session=pg_session,
        queue=mock_queue,
        principal=principal,
    )

    reconciled_job = await pg_session.get(ImportJob, app_job.id)
    assert reconciled_job is not None
    assert reconciled_job.status == ImportJobStatus.CANCELLED
    assert reconciled_job.finished_at is not None
    assert reconciled_job.checkpoint["phase"] == "cancelled_reconciled"
    assert resp_second.status == "cancelled"

    # 5. Invocazione 3: idempotenza della saga su job già terminale
    resp_third = await cancel_job_endpoint(
        job_id=app_job.id,
        session=pg_session,
        queue=mock_queue,
        principal=principal,
    )
    assert resp_third.status == "cancelled"
