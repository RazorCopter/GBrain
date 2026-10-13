from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import procrastinate
import procrastinate.exceptions
import pytest

from gbrain.config import get_settings
from gbrain.infrastructure.task_queue import GMAIL_TASK_NAME, create_procrastinate_app


@pytest.fixture(autouse=True)
def isolated_worker_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Isola l'ambiente di configurazione del worker ed evita mutazioni persistenti.

    1. monkeypatch imposta le variabili d'ambiente fittizie unicamente durante il test.
    2. get_settings.cache_clear() previene la propagazione di configurazioni cached.
    3. Il teardown pulisce get_settings e rimuove gbrain.worker da sys.modules,
       garantendo isolamento completo tra i test e prevenendo interferenze con
       la suite di configurazione o altri test.
    """
    monkeypatch.setenv("GBRAIN_POSTGRES_PASSWORD", "test-secret")
    monkeypatch.setenv("GBRAIN_AUTH_SECRET", "0" * 40)
    monkeypatch.setenv("GBRAIN_NEO4J_PASSWORD", "test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    sys.modules.pop("gbrain.worker", None)


class InstrumentedTestContext:
    """Context manager asincrono strumentato per monitorare il ciclo di vita."""

    def __init__(self, recorder: list[str]) -> None:
        self.recorder = recorder
        self.entered = False
        self.exited = False
        self.exit_exc: type[BaseException] | None = None

    async def __aenter__(self) -> InstrumentedTestContext:
        self.entered = True
        self.recorder.append("context_enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.exited = True
        self.exit_exc = exc_type
        self.recorder.append("context_exit")


@pytest.mark.asyncio
async def test_run_worker_invokes_run_worker_async_inside_open_async_context() -> None:
    """Verifica che run_worker_async() sia invocato ESCLUSIVAMENTE all'interno del context di open_async()."""
    from gbrain.worker import procrastinate_app, run_worker

    recorder: list[str] = []
    test_context = InstrumentedTestContext(recorder)

    async def fake_run_worker_async(**kwargs: Any) -> None:
        # Dimostra che il context manager è aperto quando run_worker_async è in esecuzione
        assert test_context.entered is True
        assert test_context.exited is False
        recorder.append("run_worker_async")

    with (
        patch.object(procrastinate_app, "open_async", return_value=test_context),
        patch.object(procrastinate_app, "run_worker_async", side_effect=fake_run_worker_async),
    ):
        await run_worker()

    assert recorder == ["context_enter", "run_worker_async", "context_exit"]
    assert test_context.entered is True
    assert test_context.exited is True
    assert test_context.exit_exc is None


@pytest.mark.asyncio
async def test_run_worker_closes_on_normal_exit() -> None:
    """Verifica che la risorsa venga chiusa correttamente all'uscita normale."""
    from gbrain.worker import procrastinate_app, run_worker

    recorder: list[str] = []
    test_context = InstrumentedTestContext(recorder)

    with (
        patch.object(procrastinate_app, "open_async", return_value=test_context),
        patch.object(procrastinate_app, "run_worker_async", new_callable=AsyncMock) as mock_run,
    ):
        await run_worker()
        mock_run.assert_awaited_once()

    assert test_context.exited is True
    assert test_context.exit_exc is None


@pytest.mark.asyncio
async def test_run_worker_closes_and_propagates_on_error() -> None:
    """Verifica che in caso di errore la risorsa venga chiusa e l'eccezione originale propaghi intatta."""
    from gbrain.worker import procrastinate_app, run_worker

    recorder: list[str] = []
    test_context = InstrumentedTestContext(recorder)

    with (
        patch.object(procrastinate_app, "open_async", return_value=test_context),
        patch.object(
            procrastinate_app, "run_worker_async", side_effect=RuntimeError("simulated crash")
        ),
    ):
        with pytest.raises(RuntimeError, match="simulated crash"):
            await run_worker()

    assert recorder == ["context_enter", "context_exit"]
    assert test_context.entered is True
    assert test_context.exited is True
    assert test_context.exit_exc is RuntimeError


@pytest.mark.asyncio
async def test_run_worker_closes_and_propagates_cancelled_error() -> None:
    """Verifica che la cancellazione non venga soppressa, la risorsa sia chiusa e CancelledError propaghi."""
    from gbrain.worker import procrastinate_app, run_worker

    recorder: list[str] = []
    test_context = InstrumentedTestContext(recorder)

    with (
        patch.object(procrastinate_app, "open_async", return_value=test_context),
        patch.object(procrastinate_app, "run_worker_async", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_worker()

    assert recorder == ["context_enter", "context_exit"]
    assert test_context.entered is True
    assert test_context.exited is True
    assert issubclass(test_context.exit_exc, asyncio.CancelledError)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_worker_forwards_expected_parameters() -> None:
    """Verifica che i parametri attesi del worker vengano inoltrati esattamente a run_worker_async()."""
    from gbrain.worker import WORKER_QUEUES, procrastinate_app, run_worker, settings

    captured_kwargs: dict[str, Any] = {}
    recorder: list[str] = []
    test_context = InstrumentedTestContext(recorder)

    async def fake_run_worker_async(**kwargs: Any) -> None:
        captured_kwargs.update(kwargs)

    with (
        patch.object(procrastinate_app, "open_async", return_value=test_context),
        patch.object(procrastinate_app, "run_worker_async", side_effect=fake_run_worker_async),
    ):
        await run_worker()

    assert captured_kwargs == {
        "concurrency": settings.worker_concurrency,
        "queues": ["maintenance", "outbox", "gmail"],
        "wait": True,
        "listen_notify": True,
        "name": "gbrain-worker",
    }
    assert captured_kwargs["queues"] == list(WORKER_QUEUES)
    assert "gmail" in captured_kwargs["queues"]


def test_gmail_job_and_worker_tasks_registered_on_procrastinate_app() -> None:
    """Dimostra che i task attesi (incluso il job Gmail) sono registrati sull'istanza worker."""
    from gbrain.worker import WORKER_QUEUES, procrastinate_app, settings

    tasks = procrastinate_app.tasks
    assert GMAIL_TASK_NAME in tasks, (
        f"Task {GMAIL_TASK_NAME} non registrato su worker.procrastinate_app"
    )

    gmail_task = tasks[GMAIL_TASK_NAME]
    assert gmail_task.queue == "gmail"
    assert gmail_task.queue in WORKER_QUEUES
    assert gmail_task.lock == "gmail-heavy-global"
    assert isinstance(gmail_task.retry_strategy, procrastinate.RetryStrategy)

    assert gmail_task.retry_strategy.max_attempts == settings.gmail_job_retry_budget

    # Verifica gli altri task infrastrutturali registrati
    assert "gbrain.heartbeat" in tasks
    assert "gbrain.consume_outbox_placeholder" in tasks
    assert "gbrain.purge_sessions" in tasks


def test_worker_wiring_and_instance_configuration() -> None:
    """Verifica che l'istanza Procrastinate del worker sia configurata coerentemente e senza istanze inattese."""
    from gbrain.worker import procrastinate_app as worker_app
    from gbrain.worker import settings

    # L'istanza è unica nel modulo worker
    assert worker_app is not None
    assert isinstance(worker_app, procrastinate.App)

    # La configurazione di base create_procrastinate_app usa PsycopgConnector
    fresh_app = create_procrastinate_app(settings)
    assert isinstance(fresh_app.connector, procrastinate.PsycopgConnector)
    assert fresh_app.connector._pool_args.get("conninfo") == settings.procrastinate_dsn

    # Entrambi condividono la medesima configurazione DSN
    assert isinstance(worker_app.connector, procrastinate.PsycopgConnector)
    assert worker_app.connector._pool_args.get("conninfo") == settings.procrastinate_dsn


def test_procrastinate_3_10_0_version_and_signatures() -> None:
    """Verifica di compatibilità diretta con Procrastinate 3.10.0 bloccato."""
    assert procrastinate.__version__ == "3.10.0"

    open_sig = inspect.signature(procrastinate.App.open_async)
    assert "pool" in open_sig.parameters
    # open_async restituisce AwaitableContext
    assert open_sig.return_annotation == "utils.AwaitableContext[App]"

    run_sig = inspect.signature(procrastinate.App.run_worker_async)
    assert "kwargs" in run_sig.parameters or set(run_sig.parameters.keys()) >= {"self"}


@pytest.mark.asyncio
async def test_procrastinate_app_not_open_raised_when_pool_is_closed() -> None:
    """Dimostra che PsycopgConnector solleva AppNotOpen quando il pool non è aperto prima dell'uso."""
    connector = procrastinate.PsycopgConnector(conninfo="postgresql://fake:5432/fake")
    app = procrastinate.App(connector=connector)

    # Senza aver chiamato open_async(), il connettore non ha il pool aperto
    with pytest.raises(procrastinate.exceptions.AppNotOpen):
        _ = connector.pool

    # Verifica inoltre che il context manager open_async() sia di tipo AwaitableContext
    ctx = app.open_async()
    assert isinstance(ctx, procrastinate.utils.AwaitableContext)


@pytest.mark.asyncio
async def test_procrastinate_awaitable_context_lifecycle_semantics() -> None:
    """Smoke test unitario sul comportamento del context manager nativo di Procrastinate 3.10.0."""
    actions: list[str] = []

    async def fake_open() -> None:
        actions.append("open")

    async def fake_close() -> None:
        actions.append("close")

    # 1. Uscita normale
    ctx_normal = procrastinate.utils.AwaitableContext(
        open_coro=fake_open,
        close_coro=fake_close,
        return_value="app_value",
    )
    async with ctx_normal as val:
        assert val == "app_value"
        actions.append("body_normal")
    assert actions == ["open", "body_normal", "close"]

    # 2. Uscita su eccezione
    actions.clear()
    ctx_error = procrastinate.utils.AwaitableContext(
        open_coro=fake_open,
        close_coro=fake_close,
        return_value="app_value",
    )
    with pytest.raises(ValueError, match="fail-in-body"):
        async with ctx_error:
            actions.append("body_error")
            raise ValueError("fail-in-body")
    assert actions == ["open", "body_error", "close"]

    # 3. Uscita su cancellazione
    actions.clear()
    ctx_cancel = procrastinate.utils.AwaitableContext(
        open_coro=fake_open,
        close_coro=fake_close,
        return_value="app_value",
    )
    with pytest.raises(asyncio.CancelledError):
        async with ctx_cancel:
            actions.append("body_cancel")
            raise asyncio.CancelledError
    assert actions == ["open", "body_cancel", "close"]


def test_api_enqueue_and_worker_task_registration_contract() -> None:
    """Verifica la coerenza tra il task accodato dalle API (gmail_api e sources_api) e quello registrato dal worker."""
    from gbrain.presentation import gmail_api, sources_api
    from gbrain.worker import procrastinate_app, settings

    api_task_name = getattr(gmail_api, "GMAIL_TASK_NAME", None)
    assert api_task_name == GMAIL_TASK_NAME == "gbrain.gmail_sync"
    assert GMAIL_TASK_NAME in procrastinate_app.tasks
    assert hasattr(sources_api, "trigger_source_sync")

    worker_task = procrastinate_app.tasks[GMAIL_TASK_NAME]
    assert worker_task.queue == "gmail"
    assert worker_task.lock == "gmail-heavy-global"

    # La firma della funzione del worker deve accettare i parametri forniti da API defer_async
    sig = inspect.signature(worker_task.func)
    expected_args = {"job_id", "account_id", "mode"}
    assert expected_args.issubset(sig.parameters.keys())

    # Entrambi i processi utilizzano la stessa factory create_procrastinate_app
    api_app = create_procrastinate_app(settings)
    assert type(api_app.connector) is type(procrastinate_app.connector)
    assert isinstance(api_app.connector, procrastinate.PsycopgConnector)
    assert isinstance(procrastinate_app.connector, procrastinate.PsycopgConnector)
    assert api_app.connector._pool_args.get(
        "conninfo"
    ) == procrastinate_app.connector._pool_args.get("conninfo")


@pytest.mark.asyncio
async def test_worker_gmail_sync_task_executes_job_successfully() -> None:
    """Verifica che il task registrato sulla coda 'gmail' invochi execute_gmail_job con i parametri attesi."""
    import uuid
    from unittest.mock import MagicMock

    from gbrain.worker import gmail_sync, settings

    test_job_id = str(uuid.uuid4())
    test_account_id = str(uuid.uuid4())
    test_mode = "incremental"

    mock_db = MagicMock()
    mock_db.close = AsyncMock()

    with (
        patch("gbrain.worker.Database", return_value=mock_db),
        patch("gbrain.worker.execute_gmail_job", new_callable=AsyncMock) as mock_execute,
    ):
        await gmail_sync.func(job_id=test_job_id, account_id=test_account_id, mode=test_mode)

        mock_execute.assert_awaited_once_with(
            mock_db,
            settings,
            job_id=test_job_id,
            account_id=test_account_id,
            mode=test_mode,
        )
        mock_db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_gmail_sync_task_handles_transient_error_retry() -> None:
    """Verifica che un errore transitorio incrementi il contatore di retry e lasci il job rieseguibile."""
    import uuid
    from unittest.mock import MagicMock

    from gbrain.application.gmail_jobs import GmailJobTransientError
    from gbrain.domain.models import ImportJob, ImportJobStatus
    from gbrain.worker import gmail_sync

    test_job_id = uuid.uuid4()
    test_account_id = uuid.uuid4()

    mock_job = MagicMock()
    mock_job.retry_count = 0
    mock_job.checkpoint = {"phase": "running"}
    mock_job.status = ImportJobStatus.RUNNING

    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=mock_job)
    mock_session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self) -> MagicMock:
            return mock_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_db = MagicMock()
    mock_db.session_factory.return_value = FakeSessionContext()
    mock_db.close = AsyncMock()

    with (
        patch("gbrain.worker.Database", return_value=mock_db),
        patch(
            "gbrain.worker.execute_gmail_job",
            side_effect=GmailJobTransientError("transient network timeout"),
        ),
    ):
        with pytest.raises(GmailJobTransientError, match="transient network timeout"):
            await gmail_sync.func(
                job_id=str(test_job_id),
                account_id=str(test_account_id),
                mode="full",
            )

        assert mock_job.retry_count == 1
        assert mock_job.status == ImportJobStatus.PENDING
        assert mock_job.checkpoint.get("phase") == "retry_scheduled"
        mock_session.get.assert_awaited_once_with(ImportJob, test_job_id)
        mock_session.commit.assert_awaited_once()
        mock_db.close.assert_awaited_once()
