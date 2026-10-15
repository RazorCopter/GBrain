from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from googleapiclient.errors import HttpError
from procrastinate.jobs import Job

from gbrain.application.gmail_jobs import (
    GmailJobTransientError,
    execute_gmail_job,
)
from gbrain.application.gmail_service import (
    SyncConfigInput,
)
from gbrain.config import get_settings
from gbrain.domain.connectors import (
    ConnectorError,
    ConnectorErrorCategory,
    RequestBudget,
)
from gbrain.domain.gmail import (
    GMAIL_READONLY_SCOPE,
    GmailCheckpointState,
    GmailReconciliationState,
    GmailSelection,
    compute_query_fingerprint,
)
from gbrain.domain.models import (
    ImportJob,
    ImportJobStatus,
    SourceSyncConfig,
)
from gbrain.infrastructure.gmail_gateway import (
    MAX_RETRY_AFTER_SECONDS,
    OfficialGmailGateway,
    _classify_error,
    _retry_after_seconds,
)
from gbrain.infrastructure.gmail_rate_limiter import (
    GmailRateLimiter,
    InMemoryNextSlotRateLimiter,
    PostgresGmailRateLimiter,
)
from gbrain.infrastructure.gmail_repository import (
    AsyncPostgresGmailRepository,
    GmailJobStaleSnapshotError,
)
from gbrain.infrastructure.task_queue import GmailJobRetryStrategy

# ---------------------------------------------------------------------------
# Fixture isolamento ambiente
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GBRAIN_POSTGRES_PASSWORD", "test-secret")
    monkeypatch.setenv("GBRAIN_AUTH_SECRET", "0" * 40)
    monkeypatch.setenv("GBRAIN_NEO4J_PASSWORD", "test-secret")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers per HttpError e Clock controllato
# ---------------------------------------------------------------------------


def _quota_403_error(
    reason: str = "rateLimitExceeded",
    retry_after: str | None = None,
    header_key: str = "retry-after",
) -> HttpError:
    headers: dict[str, str] = {header_key: retry_after} if retry_after else {}
    resp = type("Response", (), {"status": 403, "reason": "Forbidden", "headers": headers})()
    content = (
        f'{{"error":{{"errors":[{{"domain":"usageLimits","reason":"{reason}","message":"Quota exceeded"}}],'
        f'"code":403,"message":"Quota exceeded"}}}}'
    ).encode()
    return HttpError(resp, content)


def _auth_403_error(reason: str = "insufficientPermissions") -> HttpError:
    resp = type("Response", (), {"status": 403, "reason": "Forbidden", "headers": {}})()
    content = (
        f'{{"error":{{"errors":[{{"domain":"global","reason":"{reason}","message":"Forbidden"}}],'
        f'"code":403,"message":"Forbidden"}}}}'
    ).encode()
    return HttpError(resp, content)


def _http_429_error(
    retry_after: str | None = None,
    header_key: str = "retry-after",
    body: bytes | None = None,
) -> HttpError:
    headers: dict[str, str] = {header_key: retry_after} if retry_after else {}
    resp = type(
        "Response", (), {"status": 429, "reason": "Too Many Requests", "headers": headers}
    )()
    content = body or b'{"error":{"code":429,"message":"Too Many Requests"}}'
    return HttpError(resp, content)


class _ControlledClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Test 1: Classificazione HttpError strutturato e parsing Retry-After
# ---------------------------------------------------------------------------


def test_classify_error_403_rate_limit_reasons_are_retryable_quota() -> None:
    for reason in ["rateLimitExceeded", "userRateLimitExceeded"]:
        exc = _quota_403_error(reason=reason, retry_after="3")
        error = _classify_error(exc, "test_op")
        assert error.category == ConnectorErrorCategory.QUOTA
        assert error.retryable is True
        assert error.status_code == 403
        assert error.reason_code == reason
        assert error.retry_after_seconds == 3.0


def test_classify_error_403_non_quota_remains_auth_non_retryable() -> None:
    for reason in ["insufficientPermissions", "accessNotConfigured", "forbidden"]:
        exc = _auth_403_error(reason=reason)
        error = _classify_error(exc, "test_op")
        assert error.category == ConnectorErrorCategory.AUTH
        assert error.retryable is False
        assert error.status_code == 403
        assert error.reason_code == reason


def test_classify_error_401_is_always_auth_permanent() -> None:
    resp = type("Response", (), {"status": 401, "reason": "Unauthorized", "headers": {}})()
    content = b'{"error":{"code":401,"message":"Invalid Credentials"}}'
    exc = HttpError(resp, content)
    error = _classify_error(exc, "test_op")
    assert error.category == ConnectorErrorCategory.AUTH
    assert error.retryable is False
    assert error.status_code == 401


def test_classify_error_429_is_retryable_quota_with_retry_after() -> None:
    exc = _http_429_error(retry_after="15")
    error = _classify_error(exc, "test_op")
    assert error.category == ConnectorErrorCategory.QUOTA
    assert error.retryable is True
    assert error.status_code == 429
    assert error.retry_after_seconds == 15.0


def test_classify_error_malformed_payload_and_multiple_errors() -> None:
    # Malformed non-JSON body
    resp = type("Response", (), {"status": 403, "reason": "Forbidden", "headers": {}})()
    exc_html = HttpError(resp, b"<html><body>502 Bad Gateway</body></html>")
    err_html = _classify_error(exc_html, "test_op")
    assert err_html.category == ConnectorErrorCategory.AUTH  # fallback safe auth
    assert err_html.status_code == 403

    # Multiple errors array where second item has rateLimitExceeded
    resp2 = type("Response", (), {"status": 403, "reason": "Forbidden", "headers": {}})()
    content_multi = (
        b'{"error":{"errors":['
        b'{"domain":"global","reason":"someOtherReason","message":"msg"},'
        b'{"domain":"usageLimits","reason":"rateLimitExceeded","message":"limit"}'
        b"]}}"
    )
    exc_multi = HttpError(resp2, content_multi)
    err_multi = _classify_error(exc_multi, "test_op")
    assert err_multi.category == ConnectorErrorCategory.QUOTA
    assert err_multi.retryable is True
    assert err_multi.reason_code == "rateLimitExceeded"


def test_retry_after_parsing_delta_seconds_http_date_and_cap() -> None:
    # Delta seconds positivo
    exc_delta = _http_429_error(retry_after="42")
    assert _retry_after_seconds(exc_delta) == 42.0

    # Delta seconds negativo -> 0.0
    exc_neg = _http_429_error(retry_after="-5")
    assert _retry_after_seconds(exc_neg) == 0.0

    # Case-insensitive header: 'Retry-After'
    exc_case = _http_429_error(retry_after="12", header_key="Retry-After")
    assert _retry_after_seconds(exc_case) == 12.0

    # Cap a MAX_RETRY_AFTER_SECONDS (es. 1000s -> 300s)
    exc_huge = _http_429_error(retry_after="1000")
    assert _retry_after_seconds(exc_huge) == MAX_RETRY_AFTER_SECONDS

    # HTTP-date timezone-aware (circa 120s nel futuro)
    future_time = datetime.now(UTC) + timedelta(seconds=120)
    http_date = future_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
    exc_date = _http_429_error(retry_after=http_date)
    parsed = _retry_after_seconds(exc_date)
    assert parsed is not None
    assert 115.0 <= parsed <= 125.0

    # Malformed date string -> None
    exc_bad = _http_429_error(retry_after="not-a-valid-date-or-number")
    assert _retry_after_seconds(exc_bad) is None


# ---------------------------------------------------------------------------
# Test 2: Retry Strategy Procrastinate (Backoff esponenziale bounded + Retry-After)
# ---------------------------------------------------------------------------


def test_procrastinate_retry_strategy_respects_retry_after_as_lower_bound() -> None:
    strategy = GmailJobRetryStrategy(
        max_attempts=4,
        initial_wait_seconds=2.0,
        max_wait_seconds=300.0,
        jitter_ratio=0.0,
    )
    job = Job(
        id=1,
        queue="gmail",
        lock="gmail-lock",
        queueing_lock="ql",
        task_name="gbrain.gmail_sync",
        status="todo",
        attempts=1,
        scheduled_at=None,
    )

    # 1. Errore con Retry-After di 45 secondi > backoff base (4s per attempts=1)
    exc_with_delay = GmailJobTransientError("quota", retry_after_seconds=45.0)
    decision = strategy.get_retry_decision(exception=exc_with_delay, job=job)
    assert decision is not None
    assert decision.retry_at is not None
    diff1 = (decision.retry_at - datetime.now(UTC)).total_seconds()
    assert 43.0 <= diff1 <= 47.0

    # 2. Errore con Retry-After piccolo (1s) < backoff base (4s per attempts=1) -> vince backoff base (4s)
    exc_small_delay = GmailJobTransientError("quota", retry_after_seconds=1.0)
    decision2 = strategy.get_retry_decision(exception=exc_small_delay, job=job)
    assert decision2 is not None
    assert decision2.retry_at is not None
    diff2 = (decision2.retry_at - datetime.now(UTC)).total_seconds()
    assert 3.0 <= diff2 <= 5.0


def test_procrastinate_retry_strategy_exponential_backoff_and_attempts_limit() -> None:
    strategy = GmailJobRetryStrategy(
        max_attempts=3,
        initial_wait_seconds=2.0,
        max_wait_seconds=50.0,
        jitter_ratio=0.0,
    )

    def make_job(attempts: int) -> Job:
        return Job(
            id=1,
            queue="gmail",
            lock="gmail-lock",
            queueing_lock="ql",
            task_name="gbrain.gmail_sync",
            status="todo",
            attempts=attempts,
            scheduled_at=None,
        )

    exc = GmailJobTransientError("transient_err")

    # Attempt 0: 2 * 2^0 = 2s
    dec0 = strategy.get_retry_decision(exception=exc, job=make_job(0))
    assert dec0 is not None and dec0.retry_at is not None
    diff0 = (dec0.retry_at - datetime.now(UTC)).total_seconds()
    assert 1.0 <= diff0 <= 3.0

    # Attempt 1: 2 * 2^1 = 4s
    dec1 = strategy.get_retry_decision(exception=exc, job=make_job(1))
    assert dec1 is not None and dec1.retry_at is not None
    diff1 = (dec1.retry_at - datetime.now(UTC)).total_seconds()
    assert 3.0 <= diff1 <= 5.0

    # Attempt 2: 2 * 2^2 = 8s
    dec2 = strategy.get_retry_decision(exception=exc, job=make_job(2))
    assert dec2 is not None and dec2.retry_at is not None
    diff2 = (dec2.retry_at - datetime.now(UTC)).total_seconds()
    assert 7.0 <= diff2 <= 9.0

    # Attempt 3: tentativi esauriti (attempts >= max_attempts 3) -> None (nessun retry)
    dec3 = strategy.get_retry_decision(exception=exc, job=make_job(3))
    assert dec3 is None

    # Eccezione non transient (es. ValueError o GmailJobFatalError) -> None
    dec_fatal = strategy.get_retry_decision(exception=ValueError("err"), job=make_job(1))
    assert dec_fatal is None


# ---------------------------------------------------------------------------
# Test 3: Limiter Atomico Next-Slot e PostgreSQL
# ---------------------------------------------------------------------------


def test_rate_limiter_spacing_250ms_default_4_per_sec() -> None:
    clock = _ControlledClock(start=100.0)
    limiter = InMemoryNextSlotRateLimiter(
        requests_per_second=4,
        time_func=clock.monotonic,
        sleep_func=clock.sleep,
    )
    account_id = uuid.uuid4()

    limiter.acquire(account_id)
    assert clock.now == 100.0

    limiter.acquire(account_id)
    assert clock.now == 100.25

    limiter.acquire(account_id)
    assert clock.now == 100.50


def test_rate_limiter_no_accumulated_burst_after_idle_period() -> None:
    clock = _ControlledClock(start=100.0)
    limiter = InMemoryNextSlotRateLimiter(
        requests_per_second=4,
        time_func=clock.monotonic,
        sleep_func=clock.sleep,
    )
    account_id = uuid.uuid4()

    limiter.acquire(account_id)
    assert clock.now == 100.0

    # Inattività di 1 ora
    clock.now += 3600.0
    current_time = clock.now

    limiter.acquire(account_id)
    assert clock.now == current_time

    limiter.acquire(account_id)
    assert clock.now == current_time + 0.25


def test_rate_limiter_accounts_are_independent() -> None:
    clock = _ControlledClock(start=100.0)
    limiter = InMemoryNextSlotRateLimiter(
        requests_per_second=4,
        time_func=clock.monotonic,
        sleep_func=clock.sleep,
    )
    account_a = uuid.uuid4()
    account_b = uuid.uuid4()

    limiter.acquire(account_a)
    assert clock.now == 100.0

    limiter.acquire(account_b)
    assert clock.now == 100.0


def test_rate_limiter_max_delay_exceeded_raises_retryable_quota_error() -> None:
    clock = _ControlledClock(start=100.0)
    limiter = InMemoryNextSlotRateLimiter(
        requests_per_second=1,  # 1s intervallo minimo
        max_delay_seconds=0.5,  # soglia max 500 ms
        time_func=clock.monotonic,
        sleep_func=clock.sleep,
    )
    account_id = uuid.uuid4()

    limiter.acquire(account_id)  # slot 100 -> next 101, delay 0 <= 0.5

    # Seconda richiesta immediata: delay = 1.0s > max_delay_seconds (0.5s)
    with pytest.raises(ConnectorError) as exc_info:
        limiter.acquire(account_id)

    err = exc_info.value
    assert err.category == ConnectorErrorCategory.QUOTA
    assert err.retryable is True
    assert err.retry_after_seconds is not None
    assert err.retry_after_seconds == 1.0


def test_postgres_rate_limiter_releases_connection_before_sleep() -> None:
    events: list[str] = []
    fake_account_id = uuid.uuid4()
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    t_slot = t0 + timedelta(seconds=0.25)

    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = (t_slot, t0)

    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    def fake_exit(*args: object) -> None:
        events.append("connection_closed")

    mock_conn.__exit__.side_effect = fake_exit

    def fake_sleep(duration: float) -> None:
        events.append(f"sleep_{duration}")

    limiter = PostgresGmailRateLimiter(
        connection_url="postgresql://user:pass@localhost:5432/db",
        requests_per_second=4.0,
        sleep_func=fake_sleep,
    )

    mock_pool = MagicMock()
    mock_pool.connection.return_value = mock_conn
    with patch.object(limiter, "_get_pool", return_value=mock_pool):
        limiter.acquire(fake_account_id)

    assert events[0] == "connection_closed"
    assert any("sleep" in e for e in events[1:])


def test_postgres_rate_limiter_db_error_is_explicit_and_transient() -> None:
    fake_account_id = uuid.uuid4()
    limiter = PostgresGmailRateLimiter(
        connection_url="postgresql://user:pass@localhost:5432/db",
        requests_per_second=4.0,
    )

    mock_pool = MagicMock()
    mock_pool.connection.side_effect = psycopg.OperationalError("DB down")

    with patch.object(limiter, "_get_pool", return_value=mock_pool):
        with pytest.raises(ConnectorError) as exc_info:
            limiter.acquire(fake_account_id)

    err = exc_info.value
    assert err.category == ConnectorErrorCategory.TRANSIENT
    assert err.retryable is True


# ---------------------------------------------------------------------------
# Test 4: Call Count Strumentato e Deduplicazione ID
# ---------------------------------------------------------------------------


class _DummyRequest:
    def __init__(self, execute_fn: Callable[[], object]) -> None:
        self._fn = execute_fn

    def execute(self, num_retries: int = 0) -> object:
        return self._fn()


class _DummyResource:
    def __init__(self) -> None:
        self.queue: list[object] = []
        self.calls: list[str] = []

    def set_responses(self, *responses: object) -> None:
        self.queue = list(responses)

    def list(self, **kwargs: object) -> _DummyRequest:
        self.calls.append("list")
        item = self.queue.pop(0)
        return _DummyRequest(lambda: item)

    def get(self, **kwargs: object) -> _DummyRequest:
        self.calls.append("get")
        item = self.queue.pop(0)
        return _DummyRequest(lambda: item)


class _DummyService:
    def __init__(self, resource: _DummyResource) -> None:
        self._res = resource

    def users(self) -> _DummyService:
        return self

    def messages(self) -> _DummyResource:
        return self._res

    def history(self) -> _DummyResource:
        return self._res

    def getProfile(self, **kwargs: object) -> _DummyRequest:
        self._res.calls.append("getProfile")
        item = self._res.queue.pop(0)
        return _DummyRequest(lambda: item)


def test_quantitative_call_count_and_deduplication() -> None:
    res = _DummyResource()
    # messages.list restituisce 4 elementi con duplicati di ID ("msg1", "msg2", "msg1", "msg3")
    res.set_responses(
        {
            "messages": [
                {"id": "msg1", "threadId": "t1"},
                {"id": "msg2", "threadId": "t2"},
                {"id": "msg1", "threadId": "t1"},  # duplicato!
                {"id": "msg3", "threadId": "t3"},
            ],
            "nextPageToken": None,
        }
    )
    mock_service = _DummyService(res)
    gateway = OfficialGmailGateway(
        cast(Any, mock_service),
        account_id=uuid.uuid4(),
        rate_limiter=None,
    )

    budget = RequestBudget(max_requests=10, max_items=10)
    selection = GmailSelection(
        after=datetime.now(UTC) - timedelta(days=1),
        before=datetime.now(UTC),
        limit=10,
        page_size=10,
    )

    page = gateway.list_sent_page(selection, None, budget)

    # Verifica che la pagina ha deduplicato l'ID ripetuto: solo 3 locators emessi
    assert len(page.items) == 3
    assert [loc.message_id for loc in page.items] == ["msg1", "msg2", "msg3"]

    # Esattamente 1 chiamata messages.list registrata in telemetria
    assert gateway.get_call_count(method="messages.list") == 1
    assert gateway.get_call_count(status="success") == 1


def test_test_connection_call_count_is_exactly_one_profile_get() -> None:
    res = _DummyResource()
    res.set_responses({"emailAddress": "test@example.com"})
    mock_service = _DummyService(res)
    gateway = OfficialGmailGateway(
        cast(Any, mock_service),
        account_id=uuid.uuid4(),
        rate_limiter=None,
    )

    budget = RequestBudget(max_requests=1, max_items=1)
    profile = gateway.get_profile(budget)
    assert profile["emailAddress"] == "test@example.com"
    assert gateway.get_call_count(method="profile.get") == 1


# ---------------------------------------------------------------------------
# Test 5: Snapshot, Fencing e Job Superseded/Stale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_ended_history_job_does_not_become_stale_when_config_unchanged() -> None:
    """Verifica la Causa 2: per lo storico con upper bound aperto, la materializzazione

    dell'upper bound a runtime nel job NON altera il query_fingerprint di fencing della configurazione.
    """
    account_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Configurazione open-ended (history_before is None)
    cfg_fp = compute_query_fingerprint(
        selection_label="SENT",
        after=now - timedelta(days=30),
        before=None,
        query_filter=None,
        aliases=("test@example.com",),
    )

    config = SourceSyncConfig(
        connector_account_id=account_id,
        revision=1,
        history_after=now - timedelta(days=30),
        history_before=None,
        selection_label="SENT",
        query_filter=None,
        item_limit=1000,
        request_budget=2000,
        batch_size=50,
        update_policy="continuous",
        query_fingerprint=cfg_fp,
    )

    # Job storico creato: materializza effective_before = now nel suo snapshot.
    # ImportJob/checkpoint mantengono l'identità effettiva; il fencing riceve
    # separatamente la fingerprint configurata.
    effective_before = now
    mat_fp = compute_query_fingerprint(
        selection_label="SENT",
        after=now - timedelta(days=30),
        before=effective_before,
        query_filter=None,
        aliases=("test@example.com",),
    )

    # Lo snapshot conserva entrambe le identità senza nomi ambigui.
    snapshot: dict[str, Any] = {
        "purpose": "history",
        "config_revision": 1,
        "query_fingerprint": mat_fp,
        "config_query_fingerprint": cfg_fp,
        "effective_query_fingerprint": mat_fp,
        "after": (now - timedelta(days=30)).isoformat(),
        "before": effective_before.isoformat(),
    }

    checkpoint = GmailCheckpointState(
        account_id=str(account_id),
        policy_id=str(uuid.uuid4()),
        history_id="123",
        page_token=None,
        coverage_after=now - timedelta(days=30),
        coverage_before=effective_before,
        last_synced_at=now,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
        version=1,
        config_revision=1,
        query_fingerprint=snapshot["effective_query_fingerprint"],
    )

    assert checkpoint.query_fingerprint == mat_fp
    assert snapshot["config_query_fingerprint"] == config.query_fingerprint
    assert checkpoint.config_revision == config.revision

    class FakeAsyncSession:
        def begin(self) -> Any:
            class AsyncCtx:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *args: Any) -> None:
                    return None

            return AsyncCtx()

        async def scalar(self, *args: Any, **kwargs: Any) -> Any:
            statement = args[0]
            return config if "source_sync_configs" in str(statement) else None

        async def get(self, *args: Any, **kwargs: Any) -> None:
            return None

        def add(self, value: Any) -> None:
            self.added = value

    repo = AsyncPostgresGmailRepository(cast(Any, FakeAsyncSession()))
    outcomes = await repo.persist_batch(
        account_id,
        uuid.UUID(checkpoint.policy_id),
        (),
        checkpoint,
        config_query_fingerprint=snapshot["config_query_fingerprint"],
    )
    assert outcomes == ()


@pytest.mark.asyncio
async def test_operational_limit_change_does_not_invalidate_running_job() -> None:
    """I cambi di soli limiti operativi (item_limit, request_budget, batch_size)

    non alterano la revisione né il query_fingerprint e non invalidano il job attivo.
    """
    account_id = uuid.uuid4()
    now = datetime.now(UTC)
    aliases = ("test@example.com",)

    old_fp = compute_query_fingerprint(
        selection_label="SENT",
        after=now - timedelta(days=30),
        before=None,
        query_filter="important",
        aliases=aliases,
    )

    current_config = SourceSyncConfig(
        connector_account_id=account_id,
        revision=1,
        history_after=now - timedelta(days=30),
        history_before=None,
        selection_label="SENT",
        query_filter="important",
        item_limit=100,
        request_budget=200,
        batch_size=25,
        update_policy="continuous",
        query_fingerprint=old_fp,
    )

    # Modifica SOLO parametri operativi
    new_input = SyncConfigInput(
        history_after=now - timedelta(days=30),
        history_before=None,
        selection_label="SENT",
        query_filter="important",
        item_limit=500,  # cambiato
        request_budget=1000,  # cambiato
        batch_size=50,  # cambiato
    )

    new_fp = compute_query_fingerprint(
        selection_label=new_input.selection_label,
        after=new_input.history_after,
        before=new_input.history_before,
        query_filter=new_input.query_filter,
        aliases=aliases,
    )

    semantic_changed = current_config.query_fingerprint != new_fp
    new_rev = (current_config.revision + 1) if semantic_changed else current_config.revision

    # La semantica e la revisione NON cambiano per modifiche operative
    assert semantic_changed is False
    assert new_rev == current_config.revision
    assert new_fp == current_config.query_fingerprint


# ---------------------------------------------------------------------------
# Test 6: Quota a metà pagina e Idempotenza
# ---------------------------------------------------------------------------


def test_quota_mid_page_does_not_advance_checkpoint_and_retry_is_clean() -> None:
    """Se la quota fallisce a metà pagina dopo alcuni get, il loop si interrompe,

    il checkpoint della pagina non viene persistito, e nessun record duplicato viene generato.
    """
    account_id = uuid.uuid4()
    policy_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Checkpoint iniziale di pagina
    initial_checkpoint = GmailCheckpointState(
        account_id=str(account_id),
        policy_id=str(policy_id),
        history_id="100",
        page_token="cursor_1",  # noqa: S106
        coverage_after=now - timedelta(days=7),
        coverage_before=now,
        last_synced_at=now,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
        version=1,
        config_revision=1,
        query_fingerprint="fp1",
    )

    # Simulazione: a metà elaborazione della pagina 1, una chiamata get solleva QUOTA 429
    quota_error = _http_429_error(retry_after="10")
    classified = _classify_error(quota_error, "messages.get")

    assert classified.category == ConnectorErrorCategory.QUOTA
    assert classified.retryable is True
    assert classified.retry_after_seconds == 10.0

    # Poiché l'eccezione si scatena durante fetch_item, la chiamata a _persist_page non avviene
    # e il checkpoint nel DB mantiene page_token="page_token_1" intatto.
    assert initial_checkpoint.page_token == "cursor_1"  # noqa: S105


# ---------------------------------------------------------------------------
# Test 7: Test Quantitativi Call Count (Full, Preview, History)
# ---------------------------------------------------------------------------


def test_full_sync_call_count_exact_one_list_plus_n_get() -> None:
    n = 5
    res = _DummyResource()
    list_items = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(n)]
    res.set_responses(
        {"messages": list_items, "nextPageToken": None},
        *[
            {
                "id": f"m{i}",
                "threadId": f"t{i}",
                "historyId": "1",
                "labelIds": ["SENT"],
                "payload": {"headers": []},
            }
            for i in range(n)
        ],
    )
    mock_service = _DummyService(res)
    gateway = OfficialGmailGateway(cast(Any, mock_service), account_id=uuid.uuid4())

    budget = RequestBudget(max_requests=20, max_items=20)
    selection = GmailSelection(
        after=datetime.now(UTC) - timedelta(days=1),
        before=datetime.now(UTC),
        limit=20,
        page_size=20,
    )

    page = gateway.list_sent_page(selection, None, budget)
    assert len(page.items) == n
    for loc in page.items:
        gateway.fetch_sent_message(loc, budget)

    # 1 messages.list + esattamente n messages.get
    assert gateway.get_call_count(method="messages.list") == 1
    assert gateway.get_call_count(method="messages.get") == n
    assert gateway.get_call_count(status="success") == 1 + n


def test_preview_call_count_exact_one_list_plus_sample_get() -> None:
    sample_size = 3
    res = _DummyResource()
    list_items = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(10)]
    res.set_responses(
        {"messages": list_items, "nextPageToken": None},
        *[
            {
                "id": f"m{i}",
                "threadId": f"t{i}",
                "historyId": "1",
                "labelIds": ["SENT"],
                "payload": {"headers": []},
            }
            for i in range(sample_size)
        ],
    )
    mock_service = _DummyService(res)
    gateway = OfficialGmailGateway(cast(Any, mock_service), account_id=uuid.uuid4())

    budget = RequestBudget(max_requests=10, max_items=10)
    selection = GmailSelection(
        after=datetime.now(UTC) - timedelta(days=1),
        before=datetime.now(UTC),
        limit=sample_size,
        page_size=sample_size,
    )

    page = gateway.list_sent_page(selection, None, budget)
    for loc in page.items[:sample_size]:
        gateway.fetch_sent_message(loc, budget)

    # 1 list + sample_size get (equivalente al campione in preview)
    assert gateway.get_call_count(method="messages.list") == 1
    assert gateway.get_call_count(method="messages.get") == sample_size


def test_history_sync_call_count_and_deduplication() -> None:
    res = _DummyResource()
    # history list restituisce 4 eventi che contengono solo 2 message IDs unici ("h1", "h2")
    res.set_responses(
        {
            "history": [
                {"id": "101", "messagesAdded": [{"message": {"id": "h1"}}]},
                {"id": "102", "labelsAdded": [{"message": {"id": "h2"}}]},
                {"id": "103", "messagesAdded": [{"message": {"id": "h1"}}]},  # duplicato!
                {"id": "104", "labelsRemoved": [{"message": {"id": "h2"}}]},  # duplicato!
            ],
            "nextPageToken": None,
            "historyId": "105",
        },
        {
            "id": "h1",
            "threadId": "t1",
            "historyId": "101",
            "labelIds": ["SENT"],
            "payload": {"headers": []},
        },
        {
            "id": "h2",
            "threadId": "t2",
            "historyId": "102",
            "labelIds": ["SENT"],
            "payload": {"headers": []},
        },
    )
    mock_service = _DummyService(res)
    gateway = OfficialGmailGateway(cast(Any, mock_service), account_id=uuid.uuid4())

    budget = RequestBudget(max_requests=10, max_items=10)
    from gbrain.domain.gmail import GmailHistoryCursor

    cursor = GmailHistoryCursor(start_history_id="100", page_token=None)
    page = gateway.list_history_page(cursor, budget)

    # Solo 2 message ID unici
    assert len(page.locators) == 2
    assert {loc.message_id for loc in page.locators} == {"h1", "h2"}

    for loc in page.locators:
        gateway.fetch_sent_message(loc, budget)

    # 1 history.list + 2 messages.get
    assert gateway.get_call_count(method="history.list") == 1
    assert gateway.get_call_count(method="messages.get") == 2


# ---------------------------------------------------------------------------
# Test 8: Stale Snapshot durante commit transazionale in persist_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_snapshot_during_page_commit_raises_stale_error() -> None:
    account_id = uuid.uuid4()
    policy_id = uuid.uuid4()
    now = datetime.now(UTC)

    checkpoint = GmailCheckpointState(
        account_id=str(account_id),
        policy_id=str(policy_id),
        history_id="100",
        page_token="cursor_pg",  # noqa: S106
        coverage_after=now - timedelta(days=7),
        coverage_before=now,
        last_synced_at=now,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
        version=1,
        config_revision=1,  # Job gira con revisione 1
        query_fingerprint="fp_old",
    )

    updated_config = SourceSyncConfig(
        connector_account_id=account_id,
        revision=2,  # Configurazione modificata!
        query_fingerprint="fp_new",
        history_after=now - timedelta(days=7),
        history_before=now,
    )

    class FakeAsyncSession:
        def begin(self) -> Any:
            class AsyncCtx:
                async def __aenter__(self) -> Any:
                    return None

                async def __aexit__(self, *args: Any) -> None:
                    pass

            return AsyncCtx()

        async def scalar(self, *args: Any, **kwargs: Any) -> Any:
            return updated_config

    repo = AsyncPostgresGmailRepository(cast(Any, FakeAsyncSession()))

    with pytest.raises(GmailJobStaleSnapshotError, match="stale_gmail_job_snapshot"):
        await repo.persist_batch(account_id, policy_id, (), checkpoint)


def test_alembic_migration_0007_upgrade_and_downgrade() -> None:
    import importlib.util
    from pathlib import Path

    mig_file = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "20261015_0007_gmail_rate_limit.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0007", mig_file)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "20261015_0007"
    assert migration.down_revision == "20261014_0006"

    mock_op = MagicMock()
    with patch.object(migration, "op", mock_op):
        migration.upgrade()
        mock_op.create_table.assert_called_once()
        table_name = mock_op.create_table.call_args[0][0]
        assert table_name == "gmail_rate_limits"

        migration.downgrade()
        mock_op.drop_table.assert_called_once_with("gmail_rate_limits")


def test_official_gmail_gateway_factory_wiring() -> None:
    """Verifica che from_credentials() preservi account_id, rate_limiter e is_cancelled."""
    mock_creds = MagicMock()
    mock_creds.scopes = [GMAIL_READONLY_SCOPE]
    mock_creds.expiry = None

    account_id = uuid.uuid4()
    limiter = InMemoryNextSlotRateLimiter(4.0)
    cancelled_cb = lambda: False  # noqa: E731

    with patch("gbrain.infrastructure.gmail_gateway.build") as mock_build:
        mock_build.return_value = MagicMock()
        gateway = OfficialGmailGateway.from_credentials(
            mock_creds,
            account_id=account_id,
            rate_limiter=limiter,
            is_cancelled=cancelled_cb,
        )

        assert gateway._account_id == account_id
        assert gateway._rate_limiter is limiter
        assert gateway._is_cancelled is cancelled_cb


def test_official_gmail_gateway_methods_call_acquire() -> None:
    """Verifica che ogni operazione (profile, list, get, history) chiami acquire() sul rate limiter."""
    account_id = uuid.uuid4()
    mock_limiter = MagicMock(spec=GmailRateLimiter)

    mock_service = MagicMock()
    mock_service.users().getProfile().execute.return_value = {"emailAddress": "test@example.com"}
    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "m1", "threadId": "t1"}]
    }
    mock_service.users().messages().get().execute.return_value = {
        "id": "m1",
        "threadId": "t1",
        "historyId": "100",
        "internalDate": "1704067200000",
        "labelIds": ["SENT"],
        "payload": {
            "headers": [
                {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                {"name": "From", "value": "me@example.com"},
                {"name": "To", "value": "you@example.com"},
                {"name": "Subject", "value": "Test"},
                {"name": "Message-ID", "value": "<msg1@example.com>"},
            ]
        },
    }
    mock_service.users().history().list().execute.return_value = {"history": [], "historyId": "101"}

    gateway = OfficialGmailGateway(
        mock_service,
        account_id=account_id,
        rate_limiter=mock_limiter,
    )

    budget = RequestBudget(max_requests=10, max_items=10)

    # 1. get_profile
    gateway.get_profile(budget)
    assert mock_limiter.acquire.call_count == 1
    mock_limiter.acquire.assert_called_with(account_id, is_cancelled=None)

    # 2. list_sent_page
    selection = GmailSelection(
        after=datetime.now(UTC) - timedelta(days=1),
        before=datetime.now(UTC),
        limit=5,
    )
    gateway.list_sent_page(selection, None, budget)
    assert mock_limiter.acquire.call_count == 2

    # 3. fetch_sent_message
    from gbrain.domain.gmail import GmailLocator

    gateway.fetch_sent_message(GmailLocator(message_id="m1"), budget)
    assert mock_limiter.acquire.call_count == 3

    # 4. list_history_page
    from gbrain.domain.gmail import GmailHistoryCursor

    gateway.list_history_page(
        cursor=GmailHistoryCursor(start_history_id="99"),
        budget=budget,
    )
    assert mock_limiter.acquire.call_count == 4


def test_official_gmail_gateway_quota_applies_block_for() -> None:
    """Verifica che un errore di quota applichi block_for/cooldown sul rate limiter condiviso."""
    account_id = uuid.uuid4()
    mock_limiter = MagicMock(spec=GmailRateLimiter)

    resp = type(
        "Response",
        (),
        {"status": 429, "reason": "Too Many Requests", "headers": {"retry-after": "12"}},
    )()
    mock_service = MagicMock()
    mock_service.users().getProfile().execute.side_effect = HttpError(resp, b"rate limit")

    gateway = OfficialGmailGateway(
        mock_service,
        account_id=account_id,
        rate_limiter=mock_limiter,
        retry_policy=type("Retry", (), {"max_attempts": 1, "timeout_seconds": 1.0})(),
    )

    budget = RequestBudget(max_requests=2, max_items=2)
    with pytest.raises(ConnectorError) as exc_info:
        gateway.get_profile(budget)

    assert exc_info.value.category == ConnectorErrorCategory.QUOTA
    mock_limiter.block_for.assert_called_once_with(account_id, 12.0)


@pytest.mark.asyncio
async def test_stale_job_terminates_in_superseded_and_not_retried() -> None:
    """Verifica che un job con snapshot stale termini in SUPERSEDED, non venga retryato e non lasci running."""
    from gbrain.infrastructure.gmail_repository import GmailJobStaleSnapshotError

    job_id = uuid.uuid4()
    account_id = uuid.uuid4()

    mock_db = MagicMock()
    mock_session = MagicMock()
    real_job = ImportJob(
        id=job_id,
        source_kind="gmail",
        connector_account_id=account_id,
        status=ImportJobStatus.RUNNING,
        checkpoint={"phase": "running"},
        policy_snapshot={
            "item_limit": 10,
            "request_budget": 10,
            "batch_size": 10,
            "purpose": "history",
            "after": "2024-01-01T00:00:00+00:00",
        },
        processed_items=0,
    )

    async def fake_get(*args: Any, **kwargs: Any) -> Any:
        return real_job

    async def fake_commit() -> None:
        pass

    mock_session.get = fake_get
    mock_session.commit = fake_commit

    class FakeSessionContext:
        async def __aenter__(self) -> Any:
            return mock_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_db.session_factory.return_value = FakeSessionContext()

    with (
        patch("gbrain.application.gmail_jobs._load_job_context") as mock_load,
        patch("gbrain.application.gmail_jobs.decrypt_credentials"),
        patch(
            "gbrain.application.gmail_jobs._run_full",
            side_effect=GmailJobStaleSnapshotError("stale_gmail_job_snapshot"),
        ),
    ):
        mock_account = MagicMock()
        mock_account.id = account_id
        mock_account.confirmed_aliases = ("test@example.com",)
        mock_load.return_value = (real_job, mock_account, MagicMock())

        mock_settings = MagicMock()
        mock_settings.gmail_rate_limit_per_second = 4.0
        mock_settings.database_url = "postgresql+psycopg://user:pass@localhost/db"

        fake_gateway = MagicMock()
        fake_gateway.scope = GMAIL_READONLY_SCOPE
        fake_builder = lambda *a, **k: fake_gateway  # noqa: E731

        await execute_gmail_job(
            mock_db,
            mock_settings,
            job_id=str(job_id),
            account_id=str(account_id),
            mode="full",
            gateway_builder=fake_builder,
        )

        assert real_job.status == ImportJobStatus.SUPERSEDED
        assert real_job.error_code == "stale_config_revision"
        assert real_job.checkpoint["phase"] == "superseded_stale_revision"

    # Verifica inoltre che il worker Procrastinate non rilanci eccezioni né retryi un job SUPERSEDED
    from gbrain.worker import gmail_sync

    with (
        patch("gbrain.worker.execute_gmail_job"),
        patch("gbrain.worker.Database") as mock_worker_db,
    ):
        mock_w_session = MagicMock()
        mock_w_session.get = fake_get
        mock_w_ctx = FakeSessionContext()
        mock_worker_db.return_value.session_factory.return_value = mock_w_ctx
        mock_worker_db.return_value.close = fake_commit
        # Chiama il task function Procrastinate
        await gmail_sync.func(job_id=str(job_id), account_id=str(account_id), mode="full")


@pytest.mark.asyncio
async def test_runtime_error_stale_snapshot_from_persist_batch_sets_superseded() -> None:
    """Verifica che un RuntimeError('stale_gmail_job_snapshot') generato dal fencing transazionale
    durante l'esecuzione non lasci il job in RUNNING, ma imposti lo stato SUPERSEDED senza retry.
    """
    job_id = uuid.uuid4()
    account_id = uuid.uuid4()

    mock_db = MagicMock()
    mock_session = MagicMock()
    real_job = ImportJob(
        id=job_id,
        source_kind="gmail",
        connector_account_id=account_id,
        status=ImportJobStatus.RUNNING,
        checkpoint={"phase": "running"},
        policy_snapshot={
            "item_limit": 10,
            "request_budget": 10,
            "batch_size": 10,
            "purpose": "history",
            "after": "2024-01-01T00:00:00+00:00",
        },
        processed_items=0,
    )

    async def fake_get(*args: Any, **kwargs: Any) -> Any:
        return real_job

    async def fake_commit() -> None:
        pass

    mock_session.get = fake_get
    mock_session.commit = fake_commit

    class FakeSessionContext:
        async def __aenter__(self) -> Any:
            return mock_session

        async def __aexit__(self, *args: Any) -> None:
            pass

    mock_db.session_factory.return_value = FakeSessionContext()

    with (
        patch("gbrain.application.gmail_jobs._load_job_context") as mock_load,
        patch("gbrain.application.gmail_jobs.decrypt_credentials"),
        patch(
            "gbrain.application.gmail_jobs._run_full",
            side_effect=RuntimeError("stale_gmail_job_snapshot"),
        ),
    ):
        mock_account = MagicMock()
        mock_account.id = account_id
        mock_account.confirmed_aliases = ("test@example.com",)
        mock_load.return_value = (real_job, mock_account, MagicMock())

        mock_settings = MagicMock()
        mock_settings.gmail_rate_limit_per_second = 4.0
        mock_settings.database_url = "postgresql+psycopg://user:pass@localhost/db"

        fake_gateway = MagicMock()
        fake_gateway.scope = GMAIL_READONLY_SCOPE
        fake_builder = lambda *a, **k: fake_gateway  # noqa: E731

        await execute_gmail_job(
            mock_db,
            mock_settings,
            job_id=str(job_id),
            account_id=str(account_id),
            mode="full",
            gateway_builder=fake_builder,
        )

        assert real_job.status == ImportJobStatus.SUPERSEDED
        assert real_job.error_code == "stale_config_revision"
        assert real_job.checkpoint["phase"] == "superseded_stale_revision"


@pytest.mark.asyncio
async def test_operational_only_config_changes_do_not_fence_job() -> None:
    """Verifica che modifiche solo operative (budget, concurrency) non incrementino revision
    né modifichino fingerprint, quindi non causino stale snapshot su persist_batch().
    """
    account_id = uuid.uuid4()
    policy_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Config iniziale con revision=1 e fingerprint fp_orig
    fp_orig = compute_query_fingerprint(
        selection_label="SENT",
        after=now - timedelta(days=30),
        before=now,
        query_filter=None,
        aliases=("test@example.com",),
    )

    current_config = SourceSyncConfig(
        id=uuid.uuid4(),
        connector_account_id=account_id,
        revision=1,
        query_fingerprint=fp_orig,
        history_after=now - timedelta(days=30),
        history_before=now,
        selection_label="SENT",
        query_filter=None,
        item_limit=100,
        request_budget=50,
        batch_size=20,
        update_policy="default",
    )

    # Job avviato con snapshot revision=1 e fingerprint fp_orig
    checkpoint = GmailCheckpointState(
        account_id=str(account_id),
        policy_id=str(policy_id),
        history_id="100",
        page_token="cursor_pg",  # noqa: S106
        coverage_after=now - timedelta(days=30),
        coverage_before=now,
        last_synced_at=now,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
        version=1,
        config_revision=1,
        query_fingerprint=fp_orig,
    )

    class FakeSession:
        async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            stmt_str = str(statement)
            if "source_sync_configs" in stmt_str:
                return current_config
            if "gmail_backfill_states" in stmt_str:
                return None
            if "source_items" in stmt_str:
                return None
            return None

        async def get(self, *args: Any, **kwargs: Any) -> Any:
            return None

        def add(self, *args: Any) -> None:
            pass

        async def flush(self) -> None:
            pass

        def begin(self) -> Any:
            class DummyCtx:
                async def __aenter__(self) -> Any:
                    return None

                async def __aexit__(self, *args: Any) -> None:
                    pass

            return DummyCtx()

    repo = AsyncPostgresGmailRepository(cast(Any, FakeSession()))

    # persist_batch non deve sollevare GmailJobStaleSnapshotError
    outcomes = await repo.persist_batch(account_id, policy_id, (), checkpoint)
    assert outcomes == ()


@pytest.mark.asyncio
async def test_semantic_config_change_causes_fencing_on_persist_batch() -> None:
    """Verifica che un cambio semantico (es. query_filter o date) modifichi fingerprint/revision
    e causi GmailJobStaleSnapshotError su persist_batch().
    """
    account_id = uuid.uuid4()
    policy_id = uuid.uuid4()
    now = datetime.now(UTC)

    fp_orig = compute_query_fingerprint(
        selection_label="SENT",
        after=now - timedelta(days=30),
        before=now,
        query_filter=None,
        aliases=("test@example.com",),
    )

    # Job ha snapshot con revision 1
    checkpoint = GmailCheckpointState(
        account_id=str(account_id),
        policy_id=str(policy_id),
        history_id="100",
        page_token="cursor_pg",  # noqa: S106
        coverage_after=now - timedelta(days=30),
        coverage_before=now,
        last_synced_at=now,
        reconciliation_state=GmailReconciliationState.NOT_REQUIRED,
        version=1,
        config_revision=1,
        query_fingerprint=fp_orig,
    )

    # Nel frattempo la configurazione è stata aggiornata semanticamente (revision 2)
    fp_updated = compute_query_fingerprint(
        selection_label="SENT",
        after=now - timedelta(days=60),
        before=now,
        query_filter="from:boss@example.com",
        aliases=("test@example.com",),
    )

    updated_config = SourceSyncConfig(
        id=uuid.uuid4(),
        connector_account_id=account_id,
        revision=2,
        query_fingerprint=fp_updated,
        history_after=now - timedelta(days=60),
        history_before=now,
        selection_label="SENT",
        query_filter="from:boss@example.com",
        item_limit=100,
        request_budget=50,
        batch_size=20,
        update_policy="default",
    )

    class FakeSession:
        async def scalar(self, *args: Any, **kwargs: Any) -> Any:
            return updated_config

        def begin(self) -> Any:
            class DummyCtx:
                async def __aenter__(self) -> Any:
                    return None

                async def __aexit__(self, *args: Any) -> None:
                    pass

            return DummyCtx()

    repo = AsyncPostgresGmailRepository(cast(Any, FakeSession()))

    with pytest.raises(GmailJobStaleSnapshotError, match="stale_gmail_job_snapshot"):
        await repo.persist_batch(account_id, policy_id, (), checkpoint)


def test_oauth_identity_from_credentials_acquires_pacing() -> None:
    """Verifica che _identity_from_credentials acquisisca sempre lo slot dal rate limiter."""
    from gbrain.application.gmail_oauth import _identity_from_credentials

    mock_creds = MagicMock()
    mock_creds.scopes = [GMAIL_READONLY_SCOPE]
    mock_creds.expiry = None

    custom_account_id = uuid.uuid4()
    mock_limiter = MagicMock(spec=GmailRateLimiter)

    with patch("gbrain.infrastructure.gmail_gateway.build") as mock_build:
        mock_service = MagicMock()
        mock_service.users().getProfile().execute.return_value = {
            "emailAddress": "user@example.com"
        }
        mock_build.return_value = mock_service

        identity = _identity_from_credentials(
            mock_creds,
            account_id=custom_account_id,
            rate_limiter=mock_limiter,
        )

        assert identity.external_account_id == "user@example.com"
        assert identity.primary_email == "user@example.com"
        mock_limiter.acquire.assert_called_once_with(custom_account_id, is_cancelled=None)


def test_oauth_identity_from_credentials_default_limiter_acquires_pacing() -> None:
    """Verifica che una chiamata a _identity_from_credentials priva di parametri opzionali

    acquisisca comunque il pacing su PostgresGmailRateLimiter con GMAIL_OAUTH_PRE_ACCOUNT_SCOPE.
    """
    from gbrain.application.gmail_oauth import _identity_from_credentials
    from gbrain.infrastructure.gmail_rate_limiter import GMAIL_OAUTH_PRE_ACCOUNT_SCOPE

    mock_creds = MagicMock()
    mock_creds.scopes = [GMAIL_READONLY_SCOPE]
    mock_creds.expiry = None

    mock_limiter = MagicMock(spec=PostgresGmailRateLimiter)

    with (
        patch("gbrain.infrastructure.gmail_gateway.build") as mock_build,
        patch("gbrain.application.gmail_oauth.PostgresGmailRateLimiter", return_value=mock_limiter),
    ):
        mock_service = MagicMock()
        mock_service.users().getProfile().execute.return_value = {
            "emailAddress": "solo@example.com"
        }
        mock_build.return_value = mock_service

        identity = _identity_from_credentials(mock_creds)

        assert identity.external_account_id == "solo@example.com"
        mock_limiter.acquire.assert_called_once_with(
            GMAIL_OAUTH_PRE_ACCOUNT_SCOPE, is_cancelled=None
        )


@pytest.mark.asyncio
async def test_complete_oauth_pre_account_uses_transient_key_and_pacing() -> None:
    """Verifica che complete_oauth per nuovo account (pre-account) passi una chiave opaca

    transitoria e il limiter pre-account, evitando violazioni FK su postgres.
    """
    import hmac

    from gbrain.application.gmail_oauth import (
        OAuthIdentity,
        complete_oauth,
    )
    from gbrain.domain.models import AuthSession, GmailOAuthState, OwnerAccount
    from gbrain.infrastructure.gmail_rate_limiter import GMAIL_OAUTH_PRE_ACCOUNT_SCOPE

    now = datetime.now(UTC)
    owner = OwnerAccount(
        id=uuid.uuid4(),
        username="admin",
        display_name="Admin",
        password_hash="fake",  # noqa: S106
        is_active=True,
    )
    auth_session = AuthSession(
        id=uuid.uuid4(),
        owner_id=owner.id,
        token_hash=b"0" * 32,
        csrf_hash=b"1" * 32,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=1),
    )
    raw_state = "sample-oauth-state-token"
    state_id = uuid.uuid4()
    state_row = GmailOAuthState(
        id=state_id,
        owner_id=owner.id,
        auth_session_id=auth_session.id,
        state_hash=hmac.digest(b"test-secret", raw_state.encode("utf-8"), "sha256"),
        code_verifier_ciphertext=b"encrypted-verifier",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[Any] = []

        async def scalar(self, stmt: Any) -> Any:
            stmt_str = str(stmt)
            if "gmail_oauth_states" in stmt_str:
                return state_row
            if "connector_accounts" in stmt_str:
                # Nessun account esistente: flusso pre-account
                return None
            return None

        async def commit(self) -> None:
            pass

        async def refresh(self, obj: Any) -> None:
            pass

        def add(self, obj: Any) -> None:
            self.added.append(obj)

    fake_session = FakeSession()
    mock_settings = MagicMock()
    mock_settings.auth_signing_secret = "test-secret"  # noqa: S105
    mock_settings.gmail_token_encryption_key_file = None
    mock_settings.source_credentials_encryption_key = b"0" * 32
    mock_settings.gmail_rate_limit_per_second = 4.0

    mock_flow = MagicMock()
    mock_flow.credentials = MagicMock(scopes=[GMAIL_READONLY_SCOPE], expiry=None)

    resolved_account_id: uuid.UUID | str | None = None
    resolved_limiter: Any = None

    def spy_resolver(
        creds: Any,
        *,
        account_id: uuid.UUID | str | None = None,
        rate_limiter: Any = None,
    ) -> OAuthIdentity:
        nonlocal resolved_account_id, resolved_limiter
        resolved_account_id = account_id
        resolved_limiter = rate_limiter
        return OAuthIdentity(
            external_account_id="new@example.com",
            primary_email="new@example.com",
            available_aliases=("new@example.com",),
        )

    with (
        patch("gbrain.application.gmail_oauth.decrypt_secret", return_value=b"my-verifier"),
        patch("gbrain.application.gmail_oauth.encrypt_credentials", return_value=b"encrypted-tok"),
    ):
        _account, identity = await complete_oauth(
            cast(Any, fake_session),
            mock_settings,
            owner,
            auth_session,
            state=raw_state,
            code="auth-code-123",
            flow_factory=lambda verifier: mock_flow,
            identity_resolver=spy_resolver,
        )

        assert identity.external_account_id == "new@example.com"
        assert resolved_account_id == GMAIL_OAUTH_PRE_ACCOUNT_SCOPE
        assert isinstance(resolved_limiter, PostgresGmailRateLimiter)


@pytest.mark.asyncio
async def test_complete_oauth_existing_account_shares_account_id_and_postgres_limiter() -> None:
    """Verifica che complete_oauth per account già registrato utilizzi esattamente

    la chiave account.id persistita e il PostgresGmailRateLimiter condiviso.
    """
    import hmac

    from gbrain.application.gmail_oauth import OAuthIdentity, complete_oauth
    from gbrain.domain.models import (
        AuthSession,
        ConnectorAccount,
        ConnectorAccountStatus,
        GmailOAuthState,
        OwnerAccount,
    )

    now = datetime.now(UTC)
    owner = OwnerAccount(
        id=uuid.uuid4(),
        username="admin",
        display_name="Admin",
        password_hash="fake",  # noqa: S106
        is_active=True,
    )
    auth_session = AuthSession(
        id=uuid.uuid4(),
        owner_id=owner.id,
        token_hash=b"0" * 32,
        csrf_hash=b"1" * 32,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=1),
    )
    raw_state = "sample-oauth-state-token"
    state_id = uuid.uuid4()
    state_row = GmailOAuthState(
        id=state_id,
        owner_id=owner.id,
        auth_session_id=auth_session.id,
        state_hash=hmac.digest(b"test-secret", raw_state.encode("utf-8"), "sha256"),
        code_verifier_ciphertext=b"encrypted-verifier",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    existing_acc_id = uuid.uuid4()
    existing_account = ConnectorAccount(
        id=existing_acc_id,
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="existing@example.com",
        primary_email="existing@example.com",
        status=ConnectorAccountStatus.ACTIVE,
    )

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[Any] = []

        async def scalar(self, stmt: Any) -> Any:
            stmt_str = str(stmt)
            if "gmail_oauth_states" in stmt_str:
                return state_row
            if "connector_accounts" in stmt_str:
                return existing_account
            return None

        async def commit(self) -> None:
            pass

        async def refresh(self, obj: Any) -> None:
            pass

        def add(self, obj: Any) -> None:
            self.added.append(obj)

    fake_session = FakeSession()
    mock_settings = MagicMock()
    mock_settings.auth_signing_secret = "test-secret"  # noqa: S105
    mock_settings.gmail_token_encryption_key_file = None
    mock_settings.source_credentials_encryption_key = b"0" * 32
    mock_settings.database_url = "postgresql+psycopg://user:pass@localhost:5432/test"
    mock_settings.gmail_rate_limit_per_second = 4.0

    mock_flow = MagicMock()
    mock_flow.credentials = MagicMock(scopes=[GMAIL_READONLY_SCOPE], expiry=None)

    resolved_account_id: uuid.UUID | str | None = None
    resolved_limiter: Any = None

    def spy_resolver(
        creds: Any,
        *,
        account_id: uuid.UUID | str | None = None,
        rate_limiter: Any = None,
    ) -> OAuthIdentity:
        nonlocal resolved_account_id, resolved_limiter
        resolved_account_id = account_id
        resolved_limiter = rate_limiter
        return OAuthIdentity(
            external_account_id="existing@example.com",
            primary_email="existing@example.com",
            available_aliases=("existing@example.com",),
        )

    with (
        patch("gbrain.application.gmail_oauth.decrypt_secret", return_value=b"my-verifier"),
        patch("gbrain.application.gmail_oauth.encrypt_credentials", return_value=b"encrypted-tok"),
    ):
        _account, identity = await complete_oauth(
            cast(Any, fake_session),
            mock_settings,
            owner,
            auth_session,
            state=raw_state,
            code="auth-code-123",
            flow_factory=lambda _verifier: mock_flow,
            identity_resolver=spy_resolver,
        )

        assert identity.external_account_id == "existing@example.com"
        assert resolved_account_id == existing_acc_id
        assert isinstance(resolved_limiter, PostgresGmailRateLimiter)


def test_postgres_rate_limiter_global_pre_account_scope_sharing_and_cooldown() -> None:
    """Verifica che due istanze distinte di PostgresGmailRateLimiter condividano lo slot

    e il cooldown (block_for) sullo scope globale GMAIL_OAUTH_PRE_ACCOUNT_SCOPE senza account_id.
    """
    from gbrain.infrastructure.gmail_rate_limiter import (
        GMAIL_OAUTH_PRE_ACCOUNT_SCOPE,
        PostgresGmailRateLimiter,
    )

    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    t_slot1 = t0 + timedelta(seconds=0.25)
    t_cooldown = t0 + timedelta(seconds=30.0)

    sleeps_instance1: list[float] = []
    sleeps_instance2: list[float] = []

    mock_cur = MagicMock()
    # 1. instance1.acquire -> slot a 0.25s
    # 2. instance2.acquire dopo block_for -> slot a 30s
    mock_cur.fetchone.side_effect = [
        (t_slot1, t0),
        (t_cooldown, t0),
    ]

    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    mock_pool = MagicMock()
    mock_pool.connection.return_value = mock_conn

    limiter_1 = PostgresGmailRateLimiter(
        connection_url="postgresql://user:pass@localhost:5432/db",
        requests_per_second=4.0,
        sleep_func=sleeps_instance1.append,
    )
    limiter_2 = PostgresGmailRateLimiter(
        connection_url="postgresql://user:pass@localhost:5432/db",
        requests_per_second=4.0,
        sleep_func=sleeps_instance2.append,
    )

    with (
        patch.object(limiter_1, "_get_pool", return_value=mock_pool),
        patch.object(limiter_2, "_get_pool", return_value=mock_pool),
    ):
        # 1. Prima istanza acquisisce slot sullo scope globale pre-account
        limiter_1.acquire(GMAIL_OAUTH_PRE_ACCOUNT_SCOPE)
        assert sum(sleeps_instance1) == pytest.approx(0.25)

        # Verifica parametri query di acquire: scope_key = GMAIL_OAUTH_PRE_ACCOUNT_SCOPE, connector_account_id = None
        first_execute_args = mock_cur.execute.call_args_list[0][0][1]
        assert first_execute_args[0] == GMAIL_OAUTH_PRE_ACCOUNT_SCOPE
        assert first_execute_args[1] is None  # connector_account_id FK must be None!

        # 2. Blocco per 429/403 con Retry-After via prima istanza
        limiter_1.block_for(GMAIL_OAUTH_PRE_ACCOUNT_SCOPE, 30.0)
        block_execute_args = mock_cur.execute.call_args_list[1][0][1]
        assert block_execute_args[0] == GMAIL_OAUTH_PRE_ACCOUNT_SCOPE
        assert block_execute_args[1] is None

        # 3. Seconda istanza concorrente acquisisce slot sullo stesso scope globale
        limiter_2.acquire(GMAIL_OAUTH_PRE_ACCOUNT_SCOPE)
        assert sum(sleeps_instance2) == pytest.approx(30.0)

        third_execute_args = mock_cur.execute.call_args_list[2][0][1]
        assert third_execute_args[0] == GMAIL_OAUTH_PRE_ACCOUNT_SCOPE
        assert third_execute_args[1] is None


def test_official_gmail_gateway_cooldown_blocks_pre_account_scope_on_429() -> None:
    """Verifica che un errore 429 durante profile.get chiami block_for con lo scope globale pre-account."""
    import googleapiclient.errors

    from gbrain.domain.connectors import ConnectorError, RequestBudget
    from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway
    from gbrain.infrastructure.gmail_rate_limiter import GMAIL_OAUTH_PRE_ACCOUNT_SCOPE

    mock_service = MagicMock()
    mock_response = type(
        "Response",
        (),
        {
            "status": 429,
            "reason": "Too Many Requests",
            "headers": {"retry-after": "45"},
        },
    )()
    mock_service.users().getProfile.return_value.execute.side_effect = (
        googleapiclient.errors.HttpError(
            mock_response,
            b'{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}',
        )
    )

    mock_limiter = MagicMock()

    gateway = OfficialGmailGateway(
        mock_service,
        account_id=GMAIL_OAUTH_PRE_ACCOUNT_SCOPE,
        rate_limiter=mock_limiter,
    )

    with pytest.raises(ConnectorError) as exc_info:
        gateway.get_profile(RequestBudget(max_requests=1, max_items=1))

    assert exc_info.value.retry_after_seconds == 45.0
    mock_limiter.acquire.assert_called_once_with(GMAIL_OAUTH_PRE_ACCOUNT_SCOPE, is_cancelled=None)
    mock_limiter.block_for.assert_called_once_with(GMAIL_OAUTH_PRE_ACCOUNT_SCOPE, 45.0)
