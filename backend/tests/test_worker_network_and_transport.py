from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from pydantic import SecretStr
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.exceptions import MaxRetryError, NameResolutionError

from gbrain.application.gmail_jobs import (
    GmailJobTransientError,
    execute_gmail_job,
)
from gbrain.application.gmail_oauth import (
    GmailOAuthError,
    GmailOAuthTransientError,
    encrypt_credentials,
    refresh_credentials,
)
from gbrain.config import Settings, get_settings
from gbrain.domain.connectors import ConnectorErrorCategory
from gbrain.domain.gmail import GMAIL_READONLY_SCOPE
from gbrain.domain.models import (
    ConnectorAccount,
    ConnectorAccountStatus,
    ImportJob,
    ImportJobStatus,
)
from gbrain.infrastructure.gmail_gateway import (
    _classify_error,
    _sanitize_error_text,
    is_transient_transport_error,
)


@pytest.fixture(autouse=True)
def isolated_worker_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    monkeypatch.setenv("GBRAIN_POSTGRES_PASSWORD", "test-secret")
    monkeypatch.setenv("GBRAIN_AUTH_SECRET", "0" * 40)
    monkeypatch.setenv("GBRAIN_NEO4J_PASSWORD", "test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    sys.modules.pop("gbrain.worker", None)


def _sample_settings(tmp_path: Path) -> Settings:
    client = tmp_path / "client.json"
    key = tmp_path / "token.key"
    client.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "synthetic.apps.example.invalid",
                    "client_secret": "synthetic-client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost:8000/api/gmail/oauth/callback"],
                }
            }
        ),
        encoding="utf-8",
    )
    key.write_text("ab" * 32, encoding="utf-8")
    return Settings(
        _env_file=None,
        environment="test",
        postgres_password=SecretStr("pg-secret"),
        auth_secret=SecretStr("a" * 40),
        neo4j_password=SecretStr("neo-secret"),
        gmail_oauth_client_secret_file=client,
        gmail_token_encryption_key_file=key,
        gmail_job_retry_budget=3,
    )


def test_compose_worker_networks_and_isolation_invariants() -> None:
    project_root = Path(__file__).resolve().parents[2]
    compose_path = project_root / "compose.yaml"
    assert compose_path.exists(), "compose.yaml deve essere presente alla radice del progetto"
    docker_executable = shutil.which("docker")
    if docker_executable is None:
        pytest.skip("Docker CLI non disponibile: impossibile validare il modello Compose effettivo")

    # Eseguibile risolto localmente con shutil.which; argomenti interamente costanti.
    result = subprocess.run(  # noqa: S603
        [docker_executable, "compose", "--profile", "compact", "config", "--format", "json"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    compose_data = json.loads(result.stdout)

    services = compose_data.get("services", {})
    networks = compose_data.get("networks", {})

    # 1. Isolamento reti
    assert "backend" in networks, "Rete backend assente"
    assert "edge" in networks, "Rete edge assente"
    edge_config = networks.get("edge") or {}
    backend_config = networks.get("backend") or {}
    assert backend_config.get("internal") is True, "Rete backend deve avere internal: true"
    assert edge_config.get("internal") is not True, "Rete edge non deve essere internal: true"

    # 2. Servizio worker
    assert "worker" in services, "Servizio worker assente"
    worker = services["worker"]
    worker_networks = worker.get("networks", [])
    assert set(worker_networks) == {"backend", "edge"}, (
        f"Worker deve essere connesso a backend ed edge, trovato: {worker_networks}"
    )
    assert "ports" not in worker, "Worker non deve pubblicare porte verso l'esterno"

    # 3. Database isolati solo su backend
    for db_service_name in ("postgres", "neo4j"):
        assert db_service_name in services, f"Servizio {db_service_name} assente"
        db_service = services[db_service_name]
        db_networks = db_service.get("networks", {})
        assert set(db_networks) == {"backend"}, (
            f"{db_service_name} deve essere connesso esclusivamente a backend, trovato: {db_networks}"
        )
        assert "ports" not in db_service, f"{db_service_name} non deve pubblicare porte"


def test_sanitize_error_text_comprehensive_protection() -> None:
    raw_callback = "Calling https://gbrain.local/api/v1/sources/gmail/oauth/callback?code=4/0Abc_code&state=xyz"
    sanitized_cb = _sanitize_error_text(raw_callback)
    assert "https://gbrain.local/api/v1/sources/gmail/oauth/callback" not in sanitized_cb
    assert "4/0Abc_code" not in sanitized_cb
    assert "[REDACTED_CALLBACK_URL]" in sanitized_cb

    raw_tokens = "Tokens: ya29.a0AfH6_access, 1//04Abc_refresh, client_secret=GOCSPX-secret123"
    sanitized_tok = _sanitize_error_text(raw_tokens)
    assert "ya29.a0AfH6_access" not in sanitized_tok
    assert "1//04Abc_refresh" not in sanitized_tok
    assert "GOCSPX-secret123" not in sanitized_tok
    assert "[REDACTED_TOKEN]" in sanitized_tok
    assert "[REDACTED_REFRESH_TOKEN]" in sanitized_tok
    assert "client_secret=[REDACTED]" in sanitized_tok

    raw_cookies_email = "User analyst@example.com with Cookie: session=sess_xyz123; user=analyst"
    sanitized_ce = _sanitize_error_text(raw_cookies_email)
    assert "analyst@example.com" not in sanitized_ce
    assert "sess_xyz123" not in sanitized_ce
    assert "[REDACTED_EMAIL]" in sanitized_ce
    assert "Cookie=[REDACTED]" in sanitized_ce


def test_realistic_dns_resolution_failure_chain_is_transient() -> None:
    gai = socket.gaierror(-3, "Temporary failure in name resolution")
    dns_err = NameResolutionError("oauth2.googleapis.com", cast(Any, None), gai)
    retry_err = MaxRetryError(
        cast(Any, None), "https://oauth2.googleapis.com/token", reason=dns_err
    )
    conn_err = RequestsConnectionError(retry_err)
    transport_err = TransportError(conn_err)  # type: ignore[no-untyped-call]
    refresh_err = RefreshError(transport_err)  # type: ignore[no-untyped-call]

    assert is_transient_transport_error(refresh_err) is True
    assert is_transient_transport_error(transport_err) is True
    assert is_transient_transport_error(conn_err) is True
    assert is_transient_transport_error(dns_err) is True

    classified = _classify_error(refresh_err, "auth.refresh", history_operation=False)
    assert classified.category == ConnectorErrorCategory.TRANSIENT
    assert classified.retryable is True
    assert (
        "oauth2.googleapis.com" in classified.safe_message
        or "RefreshError" in classified.safe_message
    )


def test_permanent_invalid_grant_is_not_transient() -> None:
    permanent_err = RefreshError(  # type: ignore[no-untyped-call]
        "invalid_grant: Token has been expired or revoked.",
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
    )
    assert is_transient_transport_error(permanent_err) is False

    classified = _classify_error(permanent_err, "auth.refresh", history_operation=False)
    assert classified.category == ConnectorErrorCategory.AUTH
    assert classified.retryable is False
    assert "invalid_grant" in classified.safe_message


def test_refresh_credentials_raises_transient_error_on_dns_failure(tmp_path: Path) -> None:
    settings = _sample_settings(tmp_path)
    creds = Credentials(  # type: ignore[no-untyped-call]
        token="expired_token",  # noqa: S106
        refresh_token="1//04_fake_refresh",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="fake_client_id",
        client_secret="fake_client_secret",  # noqa: S106
        scopes=[GMAIL_READONLY_SCOPE],
        expiry=datetime.now(UTC) - timedelta(hours=1),
    )

    gai = socket.gaierror(-3, "Temporary failure in name resolution")
    dns_err = NameResolutionError("oauth2.googleapis.com", cast(Any, None), gai)
    retry_err = MaxRetryError(
        cast(Any, None), "https://oauth2.googleapis.com/token", reason=dns_err
    )
    conn_err = RequestsConnectionError(retry_err)
    transport_err = TransportError(conn_err)  # type: ignore[no-untyped-call]
    refresh_err = RefreshError(transport_err)  # type: ignore[no-untyped-call]

    with patch.object(creds, "refresh", side_effect=refresh_err):
        with pytest.raises(GmailOAuthTransientError) as exc_info:
            refresh_credentials(creds, settings)

        assert exc_info.value.code == "oauth_transport_error"
        assert "temporaneamente" in exc_info.value.safe_message.lower()
        assert "1//04_fake_refresh" not in exc_info.value.safe_message
        assert "fake_client_secret" not in exc_info.value.safe_message


def test_refresh_credentials_raises_permanent_error_on_invalid_grant(tmp_path: Path) -> None:
    settings = _sample_settings(tmp_path)
    creds = Credentials(  # type: ignore[no-untyped-call]
        token="expired_token",  # noqa: S106
        refresh_token="1//04_fake_refresh",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="fake_client_id",
        client_secret="fake_client_secret",  # noqa: S106
        scopes=[GMAIL_READONLY_SCOPE],
        expiry=datetime.now(UTC) - timedelta(hours=1),
    )

    permanent_err = RefreshError("invalid_grant: Bad Request")  # type: ignore[no-untyped-call]

    with patch.object(creds, "refresh", side_effect=permanent_err):
        with pytest.raises(GmailOAuthError) as exc_info:
            refresh_credentials(creds, settings)

        assert not isinstance(exc_info.value, GmailOAuthTransientError)
        assert exc_info.value.code == "invalid_grant"
        assert "non riuscito" in exc_info.value.safe_message.lower()


@pytest.mark.asyncio
async def test_execute_gmail_job_dns_failure_propagates_transient_error_without_account_revocation(
    tmp_path: Path,
) -> None:
    settings = _sample_settings(tmp_path)
    account_id = uuid.uuid4()
    job_id = uuid.uuid4()

    creds = Credentials(  # type: ignore[no-untyped-call]
        token="expired_token",  # noqa: S106
        refresh_token="1//04_fake_refresh",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="fake_client",
        client_secret="fake_secret",  # noqa: S106
        scopes=[GMAIL_READONLY_SCOPE],
        expiry=datetime.now(UTC) - timedelta(minutes=10),
    )
    ciphertext = encrypt_credentials(creds, settings)

    account = ConnectorAccount(
        id=account_id,
        owner_id=uuid.uuid4(),
        connector_kind="gmail",
        external_account_id="test@example.com",
        primary_email="test@example.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=ciphertext,
        oauth_token_format_version=1,
        confirmed_aliases=["test@example.com"],
    )
    job = ImportJob(
        id=job_id,
        source_kind="gmail",
        connector_account_id=account_id,
        status=ImportJobStatus.RUNNING,
        mode="full",
        retry_count=0,
        checkpoint={"phase": "started"},
        policy_snapshot={
            "after": (datetime.now(UTC) - timedelta(days=7)).isoformat(),
            "before": datetime.now(UTC).isoformat(),
            "item_limit": 50,
            "batch_size": 20,
            "request_budget": 100,
        },
    )

    mock_session = MagicMock()
    mock_session.scalar = AsyncMock(return_value=job)
    mock_session.get = AsyncMock(return_value=job)
    mock_session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self) -> MagicMock:
            return mock_session

        async def __aexit__(self, *args: object) -> None:
            pass

    mock_db = MagicMock()
    mock_db.session_factory = lambda: FakeSessionContext()

    dns_exc = GmailOAuthTransientError(
        "oauth_transport_error",
        "Rinnovo autorizzazione Gmail temporaneamente non riuscito per errore di trasporto: NameResolutionError",
    )

    with (
        patch("gbrain.application.gmail_jobs._load_job_context", return_value=(job, account, None)),
        patch("gbrain.application.gmail_jobs.refresh_credentials", side_effect=dns_exc),
    ):
        with pytest.raises(GmailJobTransientError) as captured:
            await execute_gmail_job(
                mock_db,
                settings,
                job_id=str(job_id),
                account_id=str(account_id),
                mode="full",
            )

        assert "NameResolutionError" in str(captured.value)
        # Account non deve essere revocato
        assert account.status == ConnectorAccountStatus.ACTIVE
        # Job checkpoint riceve l'errore
        assert job.status == ImportJobStatus.FAILED


@pytest.mark.asyncio
async def test_execute_gmail_job_invalid_grant_fails_permanently_no_transient_raise(
    tmp_path: Path,
) -> None:
    settings = _sample_settings(tmp_path)
    account_id = uuid.uuid4()
    job_id = uuid.uuid4()

    creds = Credentials(  # type: ignore[no-untyped-call]
        token="expired_token",  # noqa: S106
        refresh_token="1//04_fake_refresh",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="fake_client",
        client_secret="fake_secret",  # noqa: S106
        scopes=[GMAIL_READONLY_SCOPE],
        expiry=datetime.now(UTC) - timedelta(minutes=10),
    )
    ciphertext = encrypt_credentials(creds, settings)

    account = ConnectorAccount(
        id=account_id,
        owner_id=uuid.uuid4(),
        connector_kind="gmail",
        external_account_id="test@example.com",
        primary_email="test@example.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=ciphertext,
        oauth_token_format_version=1,
        confirmed_aliases=["test@example.com"],
    )
    job = ImportJob(
        id=job_id,
        source_kind="gmail",
        connector_account_id=account_id,
        status=ImportJobStatus.RUNNING,
        mode="full",
        retry_count=0,
        checkpoint={"phase": "started"},
        policy_snapshot={
            "after": (datetime.now(UTC) - timedelta(days=7)).isoformat(),
            "before": datetime.now(UTC).isoformat(),
            "item_limit": 50,
            "batch_size": 20,
            "request_budget": 100,
        },
    )

    mock_session = MagicMock()
    mock_session.scalar = AsyncMock(return_value=job)
    mock_session.get = AsyncMock(return_value=job)
    mock_session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self) -> MagicMock:
            return mock_session

        async def __aexit__(self, *args: object) -> None:
            pass

    mock_db = MagicMock()
    mock_db.session_factory = lambda: FakeSessionContext()

    permanent_exc = GmailOAuthError(
        "invalid_grant",
        "Rinnovo autorizzazione Gmail non riuscito: invalid_grant: Bad Request",
    )

    with (
        patch("gbrain.application.gmail_jobs._load_job_context", return_value=(job, account, None)),
        patch("gbrain.application.gmail_jobs.refresh_credentials", side_effect=permanent_exc),
    ):
        # Non deve sollevare GmailJobTransientError
        await execute_gmail_job(
            mock_db,
            settings,
            job_id=str(job_id),
            account_id=str(account_id),
            mode="full",
        )

        assert job.status == ImportJobStatus.FAILED


@pytest.mark.asyncio
async def test_worker_retries_transient_refresh_error_within_budget() -> None:
    from gbrain.worker import gmail_sync

    job_id = uuid.uuid4()
    account_id = uuid.uuid4()

    job = ImportJob(
        id=job_id,
        source_kind="gmail",
        connector_account_id=account_id,
        status=ImportJobStatus.RUNNING,
        retry_count=0,
        checkpoint={"phase": "running"},
    )

    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=job)
    mock_session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self) -> MagicMock:
            return mock_session

        async def __aexit__(self, *args: object) -> None:
            pass

    mock_db = MagicMock()
    mock_db.session_factory.return_value = FakeSessionContext()
    mock_db.close = AsyncMock()

    with (
        patch("gbrain.worker.Database", return_value=mock_db),
        patch(
            "gbrain.worker.execute_gmail_job",
            side_effect=GmailJobTransientError("Temporary failure in name resolution"),
        ),
    ):
        with pytest.raises(GmailJobTransientError):
            await gmail_sync(str(job_id), str(account_id), "full")

        assert job.retry_count == 1
        assert job.status == ImportJobStatus.PENDING
        assert job.finished_at is None
        assert job.checkpoint.get("phase") == "retry_scheduled"
