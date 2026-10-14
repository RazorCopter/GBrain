from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gbrain.application.import_job_runtime import (
    ImportJobCancelState,
    ImportJobConsistency,
    ImportJobRuntimeStatus,
    derive_import_job_runtime,
    sanitize_runtime_reason,
)
from gbrain.config import Settings
from gbrain.domain.models import (
    AuthSession,
    ConnectorAccount,
    ConnectorAccountStatus,
    ImportJob,
    ImportJobStatus,
    OwnerAccount,
    SourceSyncConfig,
)
from gbrain.infrastructure.procrastinate_jobs import ProcrastinateJobSnapshot
from gbrain.presentation.dependencies import (
    Principal,
    get_db_session,
    get_procrastinate,
    get_settings,
    require_csrf,
    require_principal,
)
from gbrain.presentation.gmail_api import gmail_router
from gbrain.presentation.sources_api import sources_router

DEFAULT_TEST_JOB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _make_dummy_job(
    *,
    job_id: uuid.UUID | None = None,
    status: ImportJobStatus = ImportJobStatus.PENDING,
    queue_job_id: int | None = 42,
    error_code: str | None = None,
    error_detail: str | None = None,
    cancel_requested_at: datetime | None = None,
    connector_account_id: uuid.UUID | None = None,
    source_policy_id: uuid.UUID | None = None,
) -> ImportJob:
    job = ImportJob(
        id=job_id or DEFAULT_TEST_JOB_ID,
        source_kind="gmail",
        connector_account_id=connector_account_id or uuid.uuid4(),
        source_policy_id=source_policy_id,
        queue_job_id=queue_job_id,
        status=status,
        mode="full",
        checkpoint={"phase": "running"},
        policy_snapshot={},
        processed_items=10,
        imported_items=8,
        duplicate_items=2,
        not_imported_items=0,
        error_code=error_code,
        error_detail=error_detail,
        retry_count=1,
        cancel_requested_at=cancel_requested_at,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    return job


def _make_snapshot(
    *,
    job_id: int = 42,
    status: str = "todo",
    scheduled_at: datetime | None = None,
    database_now: datetime | None = None,
    attempts: int = 1,
    blocked_by_lock: bool = False,
    abort_requested: bool = False,
    lock: str | None = "gmail-heavy-global",
    queue_name: str = "gmail",
    task_name: str = "gbrain.gmail_sync",
    has_retry_event: bool = False,
    args: dict[str, Any] | None = None,
    job: ImportJob | None = None,
) -> ProcrastinateJobSnapshot:
    now = database_now or datetime.now(UTC)
    if args is None:
        target_job_id = str(job.id) if job is not None else str(DEFAULT_TEST_JOB_ID)
        args = {"job_id": target_job_id, "mode": "full"}
    return ProcrastinateJobSnapshot(
        job_id=job_id,
        queue_name=queue_name,
        task_name=task_name,
        lock=lock,
        status=status,
        scheduled_at=scheduled_at,
        attempts=attempts,
        abort_requested=abort_requested,
        worker_id=None,
        database_now=now,
        blocked_by_lock=blocked_by_lock,
        has_retry_event=has_retry_event,
        args=args,
    )


class TestImportJobRuntimeDerivation:
    def test_scheduled_retry_when_todo_and_future_scheduled_at(self) -> None:
        db_now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
        scheduled_at = db_now + timedelta(seconds=45)
        job = _make_dummy_job(
            status=ImportJobStatus.PENDING,
            error_code="gmail_rate_limit_exceeded",
            error_detail="429 Too Many Requests - unverified backend text",
        )
        snapshot = _make_snapshot(
            status="todo",
            scheduled_at=scheduled_at,
            database_now=db_now,
            attempts=2,
        )

        runtime = derive_import_job_runtime(job, snapshot)

        assert runtime.status == ImportJobRuntimeStatus.SCHEDULED_RETRY
        assert runtime.active is True
        assert runtime.consistency == ImportJobConsistency.CONSISTENT
        assert runtime.retry is not None
        assert runtime.retry.attempts == 2
        assert runtime.retry.scheduled_at == scheduled_at
        assert runtime.retry.database_now == db_now
        assert runtime.retry.remaining_seconds == pytest.approx(45.0, abs=0.1)

        # Sanitizzazione rigorosa: solo il messaggio da allowlist, nessun dettaglio grezzo
        assert runtime.reason is not None
        assert runtime.reason.code == "gmail_rate_limit_exceeded"
        assert "Limite di velocità Google API" in runtime.reason.message
        assert "Too Many Requests" not in runtime.reason.message

        assert runtime.cancel.allowed is True
        assert runtime.cancel.state == ImportJobCancelState.AVAILABLE

    def test_transient_retry_reason_fallback_when_error_code_not_in_allowlist(self) -> None:
        db_now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
        scheduled_at = db_now + timedelta(seconds=10)
        job = _make_dummy_job(status=ImportJobStatus.PENDING, error_code="unexpected_internal_code")
        snapshot = _make_snapshot(
            status="todo",
            scheduled_at=scheduled_at,
            database_now=db_now,
        )

        runtime = derive_import_job_runtime(job, snapshot)

        assert runtime.status == ImportJobRuntimeStatus.SCHEDULED_RETRY
        assert runtime.reason is not None
        assert runtime.reason.code == "transient_retry"
        assert "Errore temporaneo; nuovo tentativo programmato" in runtime.reason.message

    def test_queued_when_todo_and_scheduled_at_is_past_or_none(self) -> None:
        db_now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
        job = _make_dummy_job(status=ImportJobStatus.PENDING)
        snapshot = _make_snapshot(
            status="todo",
            scheduled_at=db_now - timedelta(seconds=5),
            database_now=db_now,
            blocked_by_lock=False,
        )

        runtime = derive_import_job_runtime(job, snapshot)

        assert runtime.status == ImportJobRuntimeStatus.QUEUED
        assert runtime.consistency == ImportJobConsistency.CONSISTENT
        assert runtime.retry is None

    def test_blocked_when_todo_and_lock_contended(self) -> None:
        db_now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
        job = _make_dummy_job(status=ImportJobStatus.PENDING)
        snapshot = _make_snapshot(
            status="todo",
            scheduled_at=None,
            database_now=db_now,
            blocked_by_lock=True,
        )

        runtime = derive_import_job_runtime(job, snapshot)

        assert runtime.status == ImportJobRuntimeStatus.BLOCKED
        assert runtime.consistency == ImportJobConsistency.CONSISTENT
        assert runtime.queue.blocked_by_lock is True

    def test_running_and_cancelling_states(self) -> None:
        job_running = _make_dummy_job(status=ImportJobStatus.RUNNING)
        snapshot = _make_snapshot(status="doing")
        runtime_running = derive_import_job_runtime(job_running, snapshot)
        assert runtime_running.status == ImportJobRuntimeStatus.RUNNING

        job_cancelling = _make_dummy_job(
            status=ImportJobStatus.RUNNING,
            cancel_requested_at=datetime.now(UTC),
        )
        runtime_cancelling = derive_import_job_runtime(job_cancelling, snapshot)
        assert runtime_cancelling.status == ImportJobRuntimeStatus.CANCELLING
        assert runtime_cancelling.cancel.allowed is False
        assert runtime_cancelling.cancel.state == ImportJobCancelState.REQUESTED

    @pytest.mark.parametrize(
        ("app_status", "queue_status", "expected_status"),
        [
            (ImportJobStatus.COMPLETED, "succeeded", ImportJobRuntimeStatus.COMPLETED),
            (
                ImportJobStatus.COMPLETED_WITH_WARNINGS,
                "succeeded",
                ImportJobRuntimeStatus.COMPLETED_WITH_WARNINGS,
            ),
            (ImportJobStatus.SUPERSEDED, "succeeded", ImportJobRuntimeStatus.SUPERSEDED),
            (ImportJobStatus.FAILED, "failed", ImportJobRuntimeStatus.FAILED),
            (ImportJobStatus.CANCELLED, "cancelled", ImportJobRuntimeStatus.CANCELLED),
            (ImportJobStatus.CANCELLED, "succeeded", ImportJobRuntimeStatus.CANCELLED),
            (ImportJobStatus.CANCELLED, "aborted", ImportJobRuntimeStatus.CANCELLED),
        ],
    )
    def test_terminal_coherence_matrix(
        self,
        app_status: ImportJobStatus,
        queue_status: str,
        expected_status: ImportJobRuntimeStatus,
    ) -> None:
        job = _make_dummy_job(status=app_status)
        snapshot = _make_snapshot(status=queue_status)
        runtime = derive_import_job_runtime(job, snapshot)

        assert runtime.status == expected_status
        assert runtime.consistency == ImportJobConsistency.CONSISTENT
        assert runtime.active is False

    @pytest.mark.parametrize(
        ("app_status", "queue_status"),
        [
            (ImportJobStatus.RUNNING, "succeeded"),
            (ImportJobStatus.PENDING, "failed"),
            (ImportJobStatus.COMPLETED, "doing"),
            (ImportJobStatus.COMPLETED, "todo"),
            (ImportJobStatus.COMPLETED, "failed"),
            (ImportJobStatus.FAILED, "succeeded"),
        ],
    )
    def test_terminal_mismatch_matrix(
        self,
        app_status: ImportJobStatus,
        queue_status: str,
    ) -> None:
        job = _make_dummy_job(status=app_status)
        snapshot = _make_snapshot(status=queue_status)
        runtime = derive_import_job_runtime(job, snapshot)

        assert runtime.status == ImportJobRuntimeStatus.TERMINAL_MISMATCH
        assert runtime.consistency == ImportJobConsistency.TERMINAL_MISMATCH

    def test_missing_queue_handling(self) -> None:
        # 1. Job attivo ma riga coda assente
        job_active = _make_dummy_job(status=ImportJobStatus.PENDING, queue_job_id=999)
        runtime_active = derive_import_job_runtime(job_active, None)
        assert runtime_active.status == ImportJobRuntimeStatus.MISSING_QUEUE
        assert runtime_active.consistency == ImportJobConsistency.QUEUE_MISSING
        assert runtime_active.queue.present is False

        # 2. Job fallito prima dell'accodamento persistente
        job_failed_enqueue = _make_dummy_job(
            status=ImportJobStatus.FAILED,
            queue_job_id=None,
            error_code="enqueue_failed",
        )
        runtime_enqueue_failed = derive_import_job_runtime(job_failed_enqueue, None)
        assert runtime_enqueue_failed.status == ImportJobRuntimeStatus.FAILED
        assert runtime_enqueue_failed.consistency == ImportJobConsistency.QUEUE_NOT_EXPECTED

    def test_observation_failed_on_exception(self) -> None:
        job = _make_dummy_job(status=ImportJobStatus.RUNNING)
        runtime = derive_import_job_runtime(job, None, queue_error=RuntimeError("DB disconnected"))

        assert runtime.status == ImportJobRuntimeStatus.UNKNOWN
        assert runtime.consistency == ImportJobConsistency.OBSERVATION_FAILED
        assert runtime.queue.present is None
        assert runtime.cancel.allowed is False

    def test_external_abort_requested_detected_prudently(self) -> None:
        job = _make_dummy_job(status=ImportJobStatus.RUNNING)
        snapshot = _make_snapshot(status="doing", abort_requested=True)
        runtime = derive_import_job_runtime(job, snapshot)

        assert runtime.status == ImportJobRuntimeStatus.UNKNOWN
        assert runtime.consistency == ImportJobConsistency.EXTERNAL_ABORT_REQUESTED
        assert runtime.cancel.allowed is False


@pytest.fixture
def test_principal() -> Principal:
    owner = OwnerAccount(id=uuid.uuid4(), username="testowner", display_name="Test Owner")
    session = AuthSession(
        id=uuid.uuid4(),
        owner_id=owner.id,
        token_hash=b"0" * 32,
        csrf_hash=b"1" * 32,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return Principal(owner=owner, session=session)


@pytest.fixture
def mock_db_session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_procrastinate_app() -> MagicMock:
    app = MagicMock()
    app.job_manager = AsyncMock()
    return app


@pytest.fixture
def api_test_client(
    test_principal: Principal,
    mock_db_session: AsyncMock,
    mock_procrastinate_app: MagicMock,
) -> TestClient:
    app = FastAPI()
    app.include_router(gmail_router)
    app.include_router(sources_router, prefix="/api")

    test_settings = Settings(
        _env_file=None,
        environment="test",
        postgres_password=SecretStr("pg-secret"),
        auth_secret=SecretStr("0" * 40),
        neo4j_password=SecretStr("neo-secret"),
    )
    app.state.settings = test_settings

    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[require_principal] = lambda: test_principal
    app.dependency_overrides[require_csrf] = lambda: test_principal

    async def _mock_db() -> Any:
        yield mock_db_session

    app.dependency_overrides[get_db_session] = _mock_db
    app.dependency_overrides[get_procrastinate] = lambda: mock_procrastinate_app

    return TestClient(app)


class TestGmailJobApiEndpoints:
    def test_get_job_endpoint_returns_runtime_and_allows_null_source_policy(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        test_principal: Principal,
    ) -> None:
        job_id = uuid.uuid4()
        account_id = uuid.uuid4()
        job = _make_dummy_job(
            job_id=job_id,
            connector_account_id=account_id,
            source_policy_id=None,  # Policy id nullo deve comunque funzionare
            status=ImportJobStatus.PENDING,
            queue_job_id=123,
            error_code="gmail_rate_limit_exceeded",
        )
        mock_db_session.scalar.return_value = job

        db_now = datetime.now(UTC)
        scheduled_at = db_now + timedelta(seconds=60)
        snapshot = _make_snapshot(
            job_id=123,
            status="todo",
            scheduled_at=scheduled_at,
            database_now=db_now,
            job=job,
        )

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = {
            "id": snapshot.job_id,
            "queue_name": snapshot.queue_name,
            "task_name": snapshot.task_name,
            "lock": snapshot.lock,
            "status": snapshot.status,
            "scheduled_at": snapshot.scheduled_at,
            "attempts": snapshot.attempts,
            "abort_requested": snapshot.abort_requested,
            "worker_id": snapshot.worker_id,
            "database_now": snapshot.database_now,
            "blocked_by_lock": snapshot.blocked_by_lock,
            "has_retry_event": snapshot.has_retry_event,
            "args": snapshot.args,
        }
        mock_db_session.execute.return_value = mock_result

        response = api_test_client.get(f"/api/gmail/jobs/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(job_id)
        assert data["queue_job_id"] == 123
        assert "runtime" in data
        assert data["runtime"]["status"] == "scheduled_retry"
        assert data["runtime"]["active"] is True
        assert data["runtime"]["consistency"] == "consistent"
        assert data["runtime"]["reason"]["code"] == "gmail_rate_limit_exceeded"
        assert data["runtime"]["retry"]["remaining_seconds"] == pytest.approx(60.0, abs=2.0)
        assert data["runtime"]["cancel"]["allowed"] is True

    def test_get_job_endpoint_404_when_job_not_owned(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
    ) -> None:
        mock_db_session.scalar.return_value = None
        response = api_test_client.get(f"/api/gmail/jobs/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["detail"] == "Lavoro Gmail non trovato"

    def test_cancel_job_endpoint_immediate_for_todo_queue(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        mock_procrastinate_app: MagicMock,
    ) -> None:
        job_id = uuid.uuid4()
        job = _make_dummy_job(
            job_id=job_id,
            status=ImportJobStatus.PENDING,
            queue_job_id=300,
        )
        mock_db_session.scalar.return_value = job
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.return_value = True

        db_now = datetime.now(UTC)
        snap_todo = {
            "id": 300,
            "queue_name": "gmail",
            "task_name": "gbrain.gmail_sync",
            "lock": "gmail-heavy-global",
            "status": "todo",
            "scheduled_at": None,
            "attempts": 0,
            "abort_requested": False,
            "worker_id": None,
            "database_now": db_now,
            "blocked_by_lock": False,
            "has_retry_event": False,
            "args": {"job_id": str(job_id), "mode": "full"},
        }
        snap_cancelled = {
            **snap_todo,
            "status": "cancelled",
        }
        res_todo = MagicMock()
        res_todo.mappings.return_value.one_or_none.return_value = snap_todo
        res_canc = MagicMock()
        res_canc.mappings.return_value.one_or_none.return_value = snap_cancelled
        mock_db_session.execute.side_effect = [res_todo, res_canc]

        response = api_test_client.post(f"/api/gmail/jobs/{job_id}/cancel")

        assert response.status_code == 200
        # Verifica chiamata rigorosa di cancel_job_by_id_async con abort=False, delete_job=False
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.assert_awaited_once_with(
            job_id=300,
            abort=False,
            delete_job=False,
        )
        assert job.status == ImportJobStatus.CANCELLED
        assert job.finished_at is not None

        data = response.json()
        assert data["status"] == "cancelled"
        assert data["runtime"]["status"] == "cancelled"
        assert data["runtime"]["cancel"]["state"] == "completed"

    def test_cancel_job_endpoint_cooperative_for_doing_queue(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        mock_procrastinate_app: MagicMock,
    ) -> None:
        job_id = uuid.uuid4()
        job = _make_dummy_job(
            job_id=job_id,
            status=ImportJobStatus.RUNNING,
            queue_job_id=400,
        )
        mock_db_session.scalar.return_value = job

        db_now = datetime.now(UTC)
        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = {
            "id": 400,
            "queue_name": "gmail",
            "task_name": "gbrain.gmail_sync",
            "lock": "gmail-heavy-global",
            "status": "doing",
            "scheduled_at": None,
            "attempts": 1,
            "abort_requested": False,
            "worker_id": 10,
            "database_now": db_now,
            "blocked_by_lock": False,
            "has_retry_event": False,
            "args": {"job_id": str(job_id), "mode": "full"},
        }
        mock_db_session.execute.return_value = mock_result
        mock_db_session.execute.side_effect = None

        response = api_test_client.post(f"/api/gmail/jobs/{job_id}/cancel")

        assert response.status_code == 200
        # Per 'doing' non deve chiamare cancel_job_by_id_async: la cancellazione è solo cooperativa!
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.assert_not_called()
        # Il job resta RUNNING sul DB finché il worker non cooperativamente lo conclude
        assert job.status == ImportJobStatus.RUNNING
        assert job.cancel_requested_at is not None

        data = response.json()
        assert data["runtime"]["status"] == "cancelling"
        assert data["runtime"]["cancel"]["state"] == "requested"

    def test_cancel_job_endpoint_idempotent_on_already_terminal_job(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        mock_procrastinate_app: MagicMock,
    ) -> None:
        job_id = uuid.uuid4()
        job = _make_dummy_job(
            job_id=job_id,
            status=ImportJobStatus.COMPLETED,
            queue_job_id=500,
        )
        mock_db_session.scalar.return_value = job

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = {
            "id": 500,
            "queue_name": "gmail",
            "task_name": "gbrain.gmail_sync",
            "lock": "gmail-heavy-global",
            "status": "succeeded",
            "scheduled_at": None,
            "attempts": 1,
            "abort_requested": False,
            "worker_id": 10,
            "database_now": datetime.now(UTC),
            "blocked_by_lock": False,
            "has_retry_event": False,
            "args": {"job_id": str(job_id), "mode": "full"},
        }
        mock_db_session.execute.return_value = mock_result

        response = api_test_client.post(f"/api/gmail/jobs/{job_id}/cancel")

        assert response.status_code == 200
        # Nessun comando inviato a Procrastinate se il job è già terminale
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.assert_not_called()
        assert response.json()["status"] == "completed"
        assert response.json()["runtime"]["status"] == "completed"

    def test_cancel_job_procrastinate_connector_error_preserves_cooperative_marker(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        mock_procrastinate_app: MagicMock,
    ) -> None:
        job_id = uuid.uuid4()
        job = _make_dummy_job(
            job_id=job_id,
            status=ImportJobStatus.RUNNING,
            queue_job_id=600,
        )
        mock_db_session.scalar.return_value = job
        # Il connector solleva un errore di rete/connessione
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.side_effect = RuntimeError(
            "Connector offline"
        )

        snap_todo = {
            "id": 600,
            "queue_name": "gmail",
            "task_name": "gbrain.gmail_sync",
            "lock": "gmail-heavy-global",
            "status": "todo",
            "scheduled_at": None,
            "attempts": 0,
            "abort_requested": False,
            "worker_id": None,
            "database_now": datetime.now(UTC),
            "blocked_by_lock": False,
            "has_retry_event": False,
            "args": {"job_id": str(job_id), "mode": "full"},
        }
        res_initial = MagicMock()
        res_initial.mappings.return_value.one_or_none.return_value = snap_todo
        # La prima query (snapshot pre-check) ha successo; la seconda (runtime derivation) solleva eccezione
        mock_db_session.execute.side_effect = [res_initial, RuntimeError("DB snapshot error")]

        response = api_test_client.post(f"/api/gmail/jobs/{job_id}/cancel")

        assert response.status_code == 200
        # Il marker cooperativo è stato comunque registrato a DB prima del tentativo queue
        assert job.cancel_requested_at is not None
        assert response.json()["runtime"]["status"] == "unknown"
        assert response.json()["runtime"]["consistency"] == "observation_failed"

    def test_source_status_endpoint_includes_active_job(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        test_principal: Principal,
    ) -> None:
        account_id = uuid.uuid4()
        account = ConnectorAccount(
            id=account_id,
            owner_id=test_principal.owner.id,
            connector_kind="gmail",
            external_account_id="user@example.com",
            status=ConnectorAccountStatus.ACTIVE,
            oauth_token_ciphertext=b"token",
            oauth_token_format_version=1,
            primary_email="user@example.com",
            confirmed_aliases=["user@example.com"],
        )
        job = _make_dummy_job(
            connector_account_id=account_id,
            status=ImportJobStatus.PENDING,
            queue_job_id=700,
        )

        mock_db_session.get.return_value = None

        # Mock query account, app config, checkpoint, and active job in sources_api
        def _scalar_side_effect(stmt: Any) -> Any:
            sql_str = str(stmt).lower()
            if "connector_accounts" in sql_str:
                return account
            if "app_configurations" in sql_str:
                return None
            if "gmail_checkpoints" in sql_str:
                return None
            if "import_jobs" in sql_str:
                return job
            return None

        mock_db_session.scalar.side_effect = _scalar_side_effect

        db_now = datetime.now(UTC)
        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = {
            "id": 700,
            "queue_name": "gmail",
            "task_name": "gbrain.gmail_sync",
            "lock": "gmail-heavy-global",
            "status": "todo",
            "scheduled_at": db_now + timedelta(seconds=30),
            "attempts": 1,
            "abort_requested": False,
            "worker_id": None,
            "database_now": db_now,
            "blocked_by_lock": False,
            "has_retry_event": False,
            "args": {"job_id": str(job.id), "mode": "full"},
        }
        mock_db_session.execute.return_value = mock_result

        cfg = SourceSyncConfig(
            id=uuid.uuid4(),
            connector_account_id=account.id,
            revision=1,
            history_after=datetime.now(UTC) - timedelta(days=30),
            selection_label="SENT",
            item_limit=1000,
            request_budget=5000,
            batch_size=50,
            query_fingerprint="fp123",
            update_policy="continuous",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        with patch(
            "gbrain.presentation.sources_api.get_or_create_sync_config",
            new=AsyncMock(return_value=cfg),
        ):
            response = api_test_client.get("/api/sources/gmail/status")

        assert response.status_code == 200
        data = response.json()
        assert "active_job" in data
        assert data["active_job"] is not None
        assert data["active_job"]["queue_job_id"] == 700
        assert data["active_job"]["runtime"]["status"] == "scheduled_retry"
        assert data["active_job"]["runtime"]["active"] is True

    def test_cancel_job_endpoint_404_when_not_owned(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        mock_procrastinate_app: MagicMock,
    ) -> None:
        # Quando la query filtrata per owner_id ritorna None (ownership mismatch)
        mock_db_session.scalar.return_value = None
        foreign_job_id = uuid.uuid4()

        response = api_test_client.post(f"/api/gmail/jobs/{foreign_job_id}/cancel")

        assert response.status_code == 404
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.assert_not_called()

    def test_cancel_job_only_affects_targeted_queue_job_id(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        mock_procrastinate_app: MagicMock,
    ) -> None:
        target_queue_id = 999888
        job_id = uuid.uuid4()
        job = _make_dummy_job(
            job_id=job_id,
            status=ImportJobStatus.PENDING,
            queue_job_id=target_queue_id,
        )
        mock_db_session.scalar.return_value = job
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.return_value = True

        db_now = datetime.now(UTC)
        snap_todo = {
            "id": target_queue_id,
            "queue_name": "gmail",
            "task_name": "gbrain.gmail_sync",
            "lock": "gmail-heavy-global",
            "status": "todo",
            "scheduled_at": None,
            "attempts": 0,
            "abort_requested": False,
            "worker_id": None,
            "database_now": db_now,
            "blocked_by_lock": False,
            "has_retry_event": False,
            "args": {"job_id": str(job_id)},
        }
        snap_cancelled = {
            **snap_todo,
            "status": "cancelled",
        }
        res_todo = MagicMock()
        res_todo.mappings.return_value.one_or_none.return_value = snap_todo
        res_canc = MagicMock()
        res_canc.mappings.return_value.one_or_none.return_value = snap_cancelled
        mock_db_session.execute.side_effect = [res_todo, res_canc]

        response = api_test_client.post(f"/api/gmail/jobs/{job_id}/cancel")
        assert response.status_code == 200

        # Verifica rigorosa: SOLO il target queue id specificato riceve la richiesta di annullamento
        assert mock_procrastinate_app.job_manager.cancel_job_by_id_async.await_count == 1
        call_kwargs = mock_procrastinate_app.job_manager.cancel_job_by_id_async.await_args.kwargs
        assert call_kwargs["job_id"] == target_queue_id


class TestDetailedMandatoryRuntimeRequirements:
    def test_scheduled_run_when_planned_first_run_without_retry(self) -> None:
        """Una prima esecuzione programmata (attempts=0, nessun evento retry) è SCHEDULED_RUN."""
        db_now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
        scheduled_at = db_now + timedelta(minutes=10)
        job = _make_dummy_job(
            status=ImportJobStatus.PENDING,
            error_code=None,
        )
        # Job appena creato con retry_count = 0
        job.retry_count = 0

        snapshot = ProcrastinateJobSnapshot(
            job_id=101,
            queue_name="gmail",
            task_name="gbrain.gmail_sync",
            lock="gmail-heavy-global",
            status="todo",
            scheduled_at=scheduled_at,
            attempts=0,
            abort_requested=False,
            worker_id=None,
            database_now=db_now,
            blocked_by_lock=False,
            has_retry_event=False,
            args={"job_id": str(job.id)},
        )

        runtime = derive_import_job_runtime(job, snapshot)

        assert runtime.status == ImportJobRuntimeStatus.SCHEDULED_RUN
        assert runtime.active is True
        assert runtime.retry is not None
        assert runtime.retry.attempts == 0
        assert runtime.retry.remaining_seconds == pytest.approx(600.0, abs=0.1)
        # Non deve chiamarsi retry se non c'è prova di retry
        assert str(runtime.status) != str(ImportJobRuntimeStatus.SCHEDULED_RETRY)

    def test_runtime_uses_database_clock_not_host_clock(self) -> None:
        """Verifica che remaining_seconds e lo stato temporale dipendano da database_now e non dal clock host."""
        # Supponiamo che il DB clock sia 12:00 UTC
        db_now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
        scheduled_at = datetime(2026, 10, 14, 12, 1, 30, tzinfo=UTC)  # 90 secondi dopo

        job = _make_dummy_job(status=ImportJobStatus.PENDING)
        job.retry_count = 1

        snapshot = ProcrastinateJobSnapshot(
            job_id=102,
            queue_name="gmail",
            task_name="gbrain.gmail_sync",
            lock="gmail-heavy-global",
            status="todo",
            scheduled_at=scheduled_at,
            attempts=1,
            abort_requested=False,
            worker_id=None,
            database_now=db_now,
            blocked_by_lock=False,
            has_retry_event=True,
            args={"job_id": str(job.id)},
        )

        # Anche se datetime.now() dell'host fosse completamente sfasato, il runtime usa snapshot.database_now
        runtime = derive_import_job_runtime(job, snapshot)

        assert runtime.status == ImportJobRuntimeStatus.SCHEDULED_RETRY
        assert runtime.retry is not None
        assert runtime.retry.remaining_seconds == pytest.approx(90.0, abs=0.01)
        assert runtime.retry.database_now == db_now

    def test_reason_allowlist_sanitization_and_privacy_guards(self) -> None:
        """Verifica che messaggi non in allowlist o con token/PII vengano rigorosamente filtrati."""
        # Codici noti e autorizzati
        reason_transient = sanitize_runtime_reason("gmail_transient")
        assert reason_transient is not None
        assert "Google" in reason_transient.message

        reason_quota = sanitize_runtime_reason("gmail_quota")
        assert reason_quota is not None
        assert "Quota" in reason_quota.message

        # Codice arbitrario con possibile token/trace leak -> None
        leak_code = "https://accounts.google.com/o/oauth2/token?bearer=ya29.secret_token"
        sanitized = sanitize_runtime_reason(leak_code)
        assert sanitized is None

        # In caso di retry programmato, codice sconosciuto ricade sul fallback generico
        fallback = sanitize_runtime_reason(leak_code, is_scheduled_retry=True)
        assert fallback is not None
        assert fallback.code == "transient_retry"
        assert "ya29" not in fallback.message

    def test_terminal_mismatch_on_task_or_args_divergence(self) -> None:
        """Se il task_name in coda o il job_id negli args differisce da ImportJob, segnala mismatch diagnostico."""
        job = _make_dummy_job(status=ImportJobStatus.RUNNING)

        # Mismatch su task_name
        snapshot_diff_task = ProcrastinateJobSnapshot(
            job_id=201,
            queue_name="maintenance",
            task_name="gbrain.purge_sessions",
            lock=None,
            status="doing",
            scheduled_at=None,
            attempts=1,
            abort_requested=False,
            worker_id=1,
            database_now=datetime.now(UTC),
            blocked_by_lock=False,
            args={"job_id": str(job.id)},
        )
        rt1 = derive_import_job_runtime(job, snapshot_diff_task)
        assert rt1.status == ImportJobRuntimeStatus.TERMINAL_MISMATCH
        assert rt1.consistency == ImportJobConsistency.TERMINAL_MISMATCH
        assert rt1.cancel.allowed is False

        # Mismatch su job_id negli args
        snapshot_diff_args = ProcrastinateJobSnapshot(
            job_id=202,
            queue_name="gmail",
            task_name="gbrain.gmail_sync",
            lock="gmail-heavy-global",
            status="doing",
            scheduled_at=None,
            attempts=1,
            abort_requested=False,
            worker_id=1,
            database_now=datetime.now(UTC),
            blocked_by_lock=False,
            args={"job_id": str(uuid.uuid4())},  # Altro job_id!
        )
        rt2 = derive_import_job_runtime(job, snapshot_diff_args)
        assert rt2.status == ImportJobRuntimeStatus.TERMINAL_MISMATCH
        assert rt2.consistency == ImportJobConsistency.TERMINAL_MISMATCH
        assert rt2.cancel.allowed is False

    @pytest.mark.parametrize(
        ("bad_queue", "bad_args"),
        [
            ("maintenance", {"job_id": "11111111-1111-1111-1111-111111111111"}),
            ("outbox", {"job_id": "11111111-1111-1111-1111-111111111111"}),
            ("gmail", {}),  # args privi di job_id
            ("gmail", {"other_key": "val"}),
            ("gmail", {"job_id": None}),
            ("gmail", "malformed_string_args"),
        ],
    )
    def test_terminal_mismatch_on_queue_name_or_empty_or_malformed_args(
        self,
        bad_queue: str,
        bad_args: Any,
    ) -> None:
        """Verifica che derive_import_job_runtime tratti tassativamente args privi di job_id,

        code diverse da 'gmail' o args malformati come mismatch diagnostico TERMINAL_MISMATCH.
        """
        job = _make_dummy_job(status=ImportJobStatus.PENDING)
        snapshot = ProcrastinateJobSnapshot(
            job_id=303,
            queue_name=bad_queue,
            task_name="gbrain.gmail_sync",
            lock="gmail-heavy-global",
            status="todo",
            scheduled_at=None,
            attempts=0,
            abort_requested=False,
            worker_id=None,
            database_now=datetime.now(UTC),
            blocked_by_lock=False,
            args=bad_args if isinstance(bad_args, dict) else {},
        )
        runtime = derive_import_job_runtime(job, snapshot)
        assert runtime.status == ImportJobRuntimeStatus.TERMINAL_MISMATCH
        assert runtime.consistency == ImportJobConsistency.TERMINAL_MISMATCH
        assert runtime.cancel.allowed is False

    def test_cancel_job_endpoint_mismatch_raises_409_and_does_not_mutate_queue(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        mock_procrastinate_app: MagicMock,
    ) -> None:
        """Verifica che in caso di mismatch di coda/task/args, l'endpoint rifiuti la cancellazione

        con HTTP 409 Conflict SENZA toccare in alcun modo la coda Procrastinate.
        """
        job_id = uuid.uuid4()
        job = _make_dummy_job(
            job_id=job_id,
            status=ImportJobStatus.PENDING,
            queue_job_id=888,
        )
        mock_db_session.scalar.return_value = job

        # Snapshot con task_name alieno (es. manutenzione) o job_id diverso
        alien_snapshot = {
            "id": 888,
            "queue_name": "maintenance",
            "task_name": "gbrain.purge_sessions",
            "lock": None,
            "status": "todo",
            "scheduled_at": None,
            "attempts": 0,
            "abort_requested": False,
            "worker_id": None,
            "database_now": datetime.now(UTC),
            "blocked_by_lock": False,
            "has_retry_event": False,
            "args": {"job_id": str(uuid.uuid4())},
        }
        res_alien = MagicMock()
        res_alien.mappings.return_value.one_or_none.return_value = alien_snapshot
        mock_db_session.execute.return_value = res_alien
        mock_db_session.execute.side_effect = None

        response = api_test_client.post(f"/api/gmail/jobs/{job_id}/cancel")

        assert response.status_code == 409
        assert "Incongruenza" in response.json()["detail"]
        # Non deve toccare la coda aliena!
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.assert_not_called()
        assert job.status == ImportJobStatus.PENDING

    def test_cancel_job_endpoint_missing_queue_converges_to_cancelled(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        mock_procrastinate_app: MagicMock,
    ) -> None:
        """Verifica che se la riga in coda è assente (missing_queue), converge a CANCELLED

        sul DB applicativo senza chiamare cancel_job_by_id_async.
        """
        job_id = uuid.uuid4()
        job = _make_dummy_job(
            job_id=job_id,
            status=ImportJobStatus.PENDING,
            queue_job_id=999,
        )
        mock_db_session.scalar.return_value = job

        res_none = MagicMock()
        res_none.mappings.return_value.one_or_none.return_value = None
        mock_db_session.execute.return_value = res_none
        mock_db_session.execute.side_effect = None

        response = api_test_client.post(f"/api/gmail/jobs/{job_id}/cancel")

        assert response.status_code == 200
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.assert_not_called()
        assert job.status == ImportJobStatus.CANCELLED
        assert job.checkpoint["phase"] == "cancelled_missing_queue"
        data = response.json()
        assert data["status"] == "cancelled"

    def test_cancel_job_endpoint_saga_reconciliation_on_already_cancelled_queue(
        self,
        api_test_client: TestClient,
        mock_db_session: AsyncMock,
        mock_procrastinate_app: MagicMock,
    ) -> None:
        """Saga recovery: se la coda Procrastinate era già stata cancellata (es. crash precedente

        tra la cancellazione su Procrastinate e il commit sul DB applicativo), una nuova chiamata
        riconcilia l'ImportJob portandolo allo stato terminale CANCELLED.
        """
        job_id = uuid.uuid4()
        job = _make_dummy_job(
            job_id=job_id,
            status=ImportJobStatus.PENDING,
            queue_job_id=555,
        )
        mock_db_session.scalar.return_value = job

        db_now = datetime.now(UTC)
        snap_cancelled = {
            "id": 555,
            "queue_name": "gmail",
            "task_name": "gbrain.gmail_sync",
            "lock": "gmail-heavy-global",
            "status": "cancelled",  # Già cancellato in Procrastinate!
            "scheduled_at": None,
            "attempts": 0,
            "abort_requested": False,
            "worker_id": None,
            "database_now": db_now,
            "blocked_by_lock": False,
            "has_retry_event": False,
            "args": {"job_id": str(job_id), "mode": "full"},
        }
        res_cancelled = MagicMock()
        res_cancelled.mappings.return_value.one_or_none.return_value = snap_cancelled
        mock_db_session.execute.return_value = res_cancelled
        mock_db_session.execute.side_effect = None

        response = api_test_client.post(f"/api/gmail/jobs/{job_id}/cancel")

        assert response.status_code == 200
        # Nessun ulteriore comando queue necessario
        mock_procrastinate_app.job_manager.cancel_job_by_id_async.assert_not_called()
        assert job.status == ImportJobStatus.CANCELLED
        assert job.checkpoint["phase"] == "cancelled_reconciled"
        data = response.json()
        assert data["status"] == "cancelled"
        assert data["runtime"]["status"] == "cancelled"
