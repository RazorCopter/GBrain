from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from pydantic import SecretStr

from gbrain.application.crypto import (
    DecryptionError,
    decrypt_json,
    decrypt_text,
    derive_credentials_key,
    encrypt_json,
    encrypt_text,
)
from gbrain.application.gmail_oauth import (
    GmailOAuthError,
    encrypt_credentials,
    normalize_google_client_config,
)
from gbrain.config import Settings
from gbrain.domain.connectors import (
    AuthenticationMethod,
    ConnectorPage,
    ConnectorStatus,
    PageState,
    SourceCategory,
    get_source_catalog,
    get_source_descriptor,
)
from gbrain.domain.gmail import (
    GMAIL_READONLY_SCOPE,
    GmailLocator,
    GmailRawMessage,
)
from gbrain.domain.models import (
    ConnectorAccount,
    ConnectorAccountStatus,
    GmailCheckpoint,
    ImportJob,
    ImportJobStatus,
    OwnerAccount,
    SourceClientCredential,
    SourcePolicy,
)
from gbrain.infrastructure.source_credentials_repository import (
    SourceCredentialsRepository,
)
from gbrain.presentation.api import api_router
from gbrain.presentation.dependencies import (
    Principal,
    get_db_session,
    get_procrastinate,
    get_settings,
    require_csrf,
    require_principal,
)


def _test_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "postgres_password": SecretStr("pg-secret"),
        "auth_secret": SecretStr("a" * 40),
        "neo4j_password": SecretStr("neo-secret"),
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _synthetic_credentials() -> Credentials:
    return Credentials(  # type: ignore[no-untyped-call]
        token="synthetic-test-access-token",  # noqa: S106
        refresh_token="synthetic-test-refresh-token",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="synthetic-client-id.apps.googleusercontent.com",
        client_secret="synthetic-client-secret",  # noqa: S106
        scopes=[GMAIL_READONLY_SCOPE],
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )


class InMemorySession:
    """Sessione in-memory per testare i flussi SQLAlchemy dei wizard e repository."""

    def __init__(self) -> None:
        self.credentials: dict[tuple[UUID, str], SourceClientCredential] = {}
        self.accounts: dict[UUID, ConnectorAccount] = {}
        self.policies: dict[UUID, SourcePolicy] = {}
        self.jobs: dict[UUID, ImportJob] = {}
        self.checkpoints: dict[UUID, GmailCheckpoint] = {}

    async def get(self, entity: Any, ident: Any, **kwargs: Any) -> Any:
        if getattr(entity, "__tablename__", None) == "gmail_checkpoints":
            return self.checkpoints.get(ident)
        if getattr(entity, "__tablename__", None) == "connector_accounts":
            return self.accounts.get(ident)
        if getattr(entity, "__tablename__", None) == "source_policies":
            return self.policies.get(ident)
        if getattr(entity, "__tablename__", None) == "import_jobs":
            return self.jobs.get(ident)
        return None

    async def scalar(self, stmt: Any) -> Any:
        stmt_str = str(stmt).lower()
        if "source_client_credentials" in stmt_str:
            for cred in self.credentials.values():
                return cred
            return None
        if "connector_accounts" in stmt_str:
            for acc in self.accounts.values():
                return acc
            return None
        if "source_policies" in stmt_str:
            for pol in self.policies.values():
                return pol
            return None
        if "gmail_checkpoints" in stmt_str:
            for chk in self.checkpoints.values():
                return chk
            return None
        if "import_jobs" in stmt_str:
            # Ritorna l'ultimo job attivo (se presente)
            for job in reversed(list(self.jobs.values())):
                if job.status == ImportJobStatus.PENDING:
                    return job
            return None
        return None

    async def execute(self, stmt: Any) -> Any:
        class _ExecResult:
            def all(self) -> list[Any]:
                return []

            def __iter__(self) -> Any:
                return iter(())

        return _ExecResult()

    async def scalars(self, stmt: Any) -> Any:
        class _Result:
            def __iter__(self) -> Any:
                return iter(())

            def all(self) -> list[Any]:
                return []

        return _Result()

    def begin(self) -> Any:
        class _Context:
            async def __aenter__(self) -> None:
                pass

            async def __aexit__(self, *args: Any) -> None:
                pass

        return _Context()

    def add(self, obj: Any) -> None:
        if isinstance(obj, SourceClientCredential):
            self.credentials[(obj.owner_id, obj.source_kind)] = obj
        elif isinstance(obj, ConnectorAccount):
            self.accounts[obj.id] = obj
        elif isinstance(obj, SourcePolicy):
            self.policies[obj.id] = obj
        elif isinstance(obj, ImportJob):
            self.jobs[obj.id] = obj
        elif isinstance(obj, GmailCheckpoint):
            self.checkpoints[obj.connector_account_id] = obj

    async def delete(self, obj: Any) -> None:
        if isinstance(obj, SourceClientCredential):
            self.credentials.pop((obj.owner_id, obj.source_kind), None)
        elif isinstance(obj, ConnectorAccount):
            self.accounts.pop(obj.id, None)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def refresh(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()


# =========================================================================
# 1. Test Crittografia AES-256-GCM e derivazione chiavi
# =========================================================================


def test_crypto_roundtrip_and_authentication() -> None:
    key = derive_credentials_key("master-auth-secret-super-secure-32chars")
    assert len(key) == 32

    # Roundtrip string
    raw_str = "client-secret-very-confidential"
    encrypted_str = encrypt_text(raw_str, key)
    assert encrypted_str != raw_str.encode("utf-8")
    assert decrypt_text(encrypted_str, key) == raw_str

    # Roundtrip JSON
    raw_dict = {"client_id": "test-id", "client_secret": "test-secret", "extra": 123}
    encrypted_json = encrypt_json(raw_dict, key)
    decrypted_dict = decrypt_json(encrypted_json, key)
    assert decrypted_dict == raw_dict

    # Verifica tamper e chiave errata
    tampered = bytearray(encrypted_json)
    tampered[-1] ^= 0xFF
    with pytest.raises(DecryptionError):
        decrypt_json(bytes(tampered), key)

    wrong_key = derive_credentials_key("another-different-secret-key-32ch")
    with pytest.raises(DecryptionError):
        decrypt_json(encrypted_json, wrong_key)


# =========================================================================
# 2. Test Normalizzazione e Validazione Credenziali Google Console
# =========================================================================


def test_normalize_google_client_config() -> None:
    # Formato standard web
    web_cfg = {
        "web": {
            "client_id": "google-client-id-123.apps.googleusercontent.com",
            "client_secret": "GOCSPX-secret123",
            "redirect_uris": ["http://localhost:8000/callback"],
        }
    }
    normalized = normalize_google_client_config(web_cfg)
    assert "web" in normalized
    assert normalized["web"]["client_id"] == "google-client-id-123.apps.googleusercontent.com"
    assert normalized["web"]["client_secret"] == "GOCSPX-secret123"  # noqa: S105

    # Formato installed (desktop/native)
    inst_cfg = {
        "installed": {
            "client_id": "installed-id.apps.googleusercontent.com",
            "client_secret": "GOCSPX-inst123",
            "redirect_uris": [],
        }
    }
    normalized_inst = normalize_google_client_config(
        inst_cfg, redirect_uri="http://localhost:8000/api/sources/gmail/oauth/callback"
    )
    assert "installed" in normalized_inst
    assert normalized_inst["installed"]["client_id"] == "installed-id.apps.googleusercontent.com"
    assert (
        normalized_inst["installed"]["redirect_uris"][0]
        == "http://localhost:8000/api/sources/gmail/oauth/callback"
    )

    # Formato flat dictionary
    flat_cfg = {
        "client_id": "flat-id.apps.googleusercontent.com",
        "client_secret": "flat-secret",
    }
    normalized_flat = normalize_google_client_config(flat_cfg)
    assert "web" in normalized_flat
    assert normalized_flat["web"]["client_id"] == "flat-id.apps.googleusercontent.com"

    # Errori di validazione struttura o chiavi mancanti
    with pytest.raises(GmailOAuthError, match="Struttura"):
        normalize_google_client_config({"unsupported_key": {}})

    with pytest.raises(GmailOAuthError, match="mancanti"):
        normalize_google_client_config({"web": {"client_id": "", "client_secret": ""}})


# =========================================================================
# 3. Test Repository Credenziali: Salvataggio, Cifratura, Sovrascrittura e Ricifratura
# =========================================================================


@pytest.mark.asyncio
async def test_source_credentials_repository_lifecycle() -> None:
    session = InMemorySession()
    owner_id = uuid4()
    settings = _test_settings()
    key = settings.source_credentials_encryption_key

    raw_payload = {
        "web": {
            "client_id": "abc-owner.apps.googleusercontent.com",
            "client_secret": "GOCSPX-super-secret",
        }
    }
    encrypted_blob = encrypt_json(raw_payload, key)

    # Verifica che il secret in chiaro NON appaia nel ciphertext
    assert b"GOCSPX-super-secret" not in encrypted_blob

    # 1. Salvataggio
    saved = await SourceCredentialsRepository.save_credentials(
        session,  # type: ignore[arg-type]
        owner_id=owner_id,
        source_kind="gmail",
        auth_method="oauth2",
        client_type="web",
        client_id="abc-owner.apps.googleusercontent.com",
        encrypted_credentials=encrypted_blob,
        configured_redirect_uri="http://localhost:8000/callback",
    )
    assert saved.owner_id == owner_id
    assert saved.source_kind == "gmail"
    assert len(session.credentials) == 1

    # 2. Recupero e decifratura
    retrieved = await SourceCredentialsRepository.get_credentials(
        cast(Any, session),
        owner_id=owner_id,
        source_kind="gmail",
    )
    assert retrieved is not None
    decrypted = decrypt_json(retrieved.encrypted_credentials, key)
    assert decrypted["web"]["client_secret"] == "GOCSPX-super-secret"  # noqa: S105

    # 3. Sovrascrittura protetta (aggiornamento senza duplicazione)
    updated_payload = {
        "web": {
            "client_id": "abc-owner.apps.googleusercontent.com",
            "client_secret": "GOCSPX-new-secret",
        }
    }
    updated_blob = encrypt_json(updated_payload, key)
    updated = await SourceCredentialsRepository.save_credentials(
        cast(Any, session),
        owner_id=owner_id,
        source_kind="gmail",
        auth_method="oauth2",
        client_type="web",
        client_id="abc-owner.apps.googleusercontent.com",
        encrypted_credentials=updated_blob,
        configured_redirect_uri="http://localhost:8000/callback2",
    )
    assert len(session.credentials) == 1
    assert updated.configured_redirect_uri == "http://localhost:8000/callback2"

    # 4. Ricifratura delle credenziali (rotazione chiavi)
    new_key = derive_credentials_key("new-master-key-32chars-rotation")
    rotated = await SourceCredentialsRepository.rotate_credentials(
        cast(Any, session),
        owner_id=owner_id,
        source_kind="gmail",
        old_key=key,
        new_key=new_key,
    )
    assert rotated is not None
    decrypted_rotated = decrypt_json(rotated.encrypted_credentials, new_key)
    assert decrypted_rotated["web"]["client_secret"] == "GOCSPX-new-secret"  # noqa: S105

    # 5. Cancellazione
    deleted = await SourceCredentialsRepository.delete_credentials(
        cast(Any, session),
        owner_id=owner_id,
        source_kind="gmail",
    )
    assert deleted is True
    assert len(session.credentials) == 0


# =========================================================================
# 4. Test Catalogo delle 9 Fonti e Descrittori
# =========================================================================


def test_source_catalog_complete_metadata() -> None:
    catalog = get_source_catalog()
    assert len(catalog) == 9

    # Verifica Gmail (Unica Fully Available in questa fase)
    gmail_desc = get_source_descriptor("gmail")
    assert gmail_desc is not None
    assert gmail_desc.id == "gmail"
    assert gmail_desc.status == ConnectorStatus.FULLY_AVAILABLE
    assert gmail_desc.test_available is True
    assert gmail_desc.category == SourceCategory.EMAIL_COMMUNICATIONS
    assert gmail_desc.auth_method == AuthenticationMethod.OAUTH2_AUTHORIZATION_CODE
    assert gmail_desc.sync_capabilities is not None
    assert gmail_desc.sync_capabilities.read_only is True
    assert gmail_desc.import_policy is not None
    assert gmail_desc.import_policy.label_restriction == "SENT"

    # Gli altri 8 connettori devono essere coming_soon
    coming_soon_ids = [
        "google_drive",
        "google_calendar",
        "immich",
        "synology",
        "local_documents",
        "github",
        "chatgpt_export",
        "omniroute",
    ]
    for cid in coming_soon_ids:
        desc = get_source_descriptor(cid)
        assert desc is not None, f"Mancante descrittore per {cid}"
        assert desc.status == ConnectorStatus.COMING_SOON
        assert desc.test_available is False


# =========================================================================
# 5. Suite Test Completa API /api/sources/*
# =========================================================================


def _build_test_client(
    *,
    db_session: InMemorySession | None = None,
    settings: Settings | None = None,
    owner: OwnerAccount | None = None,
    auth_session: Any = None,
    procrastinate_mock: Any = None,
) -> tuple[TestClient, InMemorySession, OwnerAccount]:
    from gbrain.presentation.gmail_api import gmail_router

    app = FastAPI()
    app.include_router(api_router)
    app.include_router(gmail_router)

    session = db_session or InMemorySession()
    test_owner = owner or OwnerAccount(
        id=uuid4(),
        owner_slot=1,
        username="admin",
        display_name="Amministratore",
        password_hash="hash",  # noqa: S106
    )
    test_settings = settings or _test_settings()

    async def override_session() -> Any:
        return session

    async def override_principal() -> Any:
        return Principal(owner=test_owner, session=auth_session)

    async def override_csrf() -> Any:
        return Principal(owner=test_owner, session=auth_session)

    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[require_principal] = override_principal
    app.dependency_overrides[require_csrf] = override_csrf

    if procrastinate_mock is not None:
        app.dependency_overrides[get_procrastinate] = lambda: procrastinate_mock

    client = TestClient(app)
    return client, session, test_owner


def test_sources_catalog_and_status_endpoints() -> None:
    client, _, _ = _build_test_client()

    # GET /api/sources/catalog
    resp = client.get("/api/sources/catalog")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 9
    assert len(data["items"]) == 9
    gmail_item = next(it for it in data["items"] if it["id"] == "gmail")
    assert gmail_item["status"] == "fully_available"
    assert gmail_item["availability"] == "fully_available"

    # GET /api/sources/gmail/status (localhost: nessun warning)
    resp_gmail = client.get("/api/sources/gmail/status", headers={"host": "localhost:8000"})
    assert resp_gmail.status_code == 200
    status_data = resp_gmail.json()
    assert status_data["source_id"] == "gmail"
    assert status_data["availability"] == "fully_available"
    assert status_data["status"] == "not_configured"
    assert status_data["configured"] is False
    assert status_data["lan_warning"] is None

    # GET /api/sources/gmail/status con IP privato LAN su HTTP: warning rilevato
    resp_lan = client.get("/api/sources/gmail/status", headers={"host": "192.168.1.120:8000"})
    assert resp_lan.status_code == 200
    lan_data = resp_lan.json()
    assert lan_data["lan_warning"] is not None
    assert lan_data["lan_warning"]["is_private_ip"] is True
    assert lan_data["lan_warning"]["warning_code"] == "private_ip_http_unsupported_by_google"

    # GET /api/sources/unknown/status -> 404
    resp_unknown = client.get("/api/sources/non_existent/status")
    assert resp_unknown.status_code == 404


def test_sources_credentials_validation_and_errors() -> None:
    client, session, _ = _build_test_client()

    # 1. File JSON valido (formato web)
    valid_web_json = json.dumps(
        {
            "web": {
                "client_id": "google-web-123.apps.googleusercontent.com",
                "client_secret": "GOCSPX-secret1",
                "redirect_uris": ["http://localhost:8000/api/sources/gmail/oauth/callback"],
            }
        }
    )
    resp_ok = client.post(
        "/api/sources/gmail/credentials",
        json={"raw_json": valid_web_json},
    )
    assert resp_ok.status_code == 200
    res_data = resp_ok.json()
    assert res_data["client_type"] == "web"
    assert "google-web-1" in res_data["client_id"]
    assert len(session.credentials) == 1

    # 2. File malformato (non JSON) -> 400
    resp_bad_json = client.post(
        "/api/sources/gmail/credentials",
        json={"raw_json": "{not valid json syntax:"},
    )
    assert resp_bad_json.status_code == 400
    assert "non è valido" in resp_bad_json.json()["detail"]

    # 3. File non-dict (array JSON o tipo errato) -> 400
    resp_array = client.post(
        "/api/sources/gmail/credentials",
        json={"raw_json": "[1, 2, 3]"},
    )
    assert resp_array.status_code == 400

    # 4. File troppo grande > 1MB -> 413
    oversized = "a" * (1_048_576 + 50)
    resp_large = client.post(
        "/api/sources/gmail/credentials",
        json={"raw_json": oversized},
    )
    assert resp_large.status_code == 413
    assert "supera la dimensione massima" in resp_large.json()["detail"]

    # 5. Mancanti client_secret o client_id -> 400
    resp_missing_keys = client.post(
        "/api/sources/gmail/credentials",
        json={"raw_json": json.dumps({"web": {"client_id": "id-only"}})},
    )
    assert resp_missing_keys.status_code == 400
    assert "mancanti" in resp_missing_keys.json()["detail"].lower()

    # 6. Payload vuoto -> 422
    resp_empty = client.post("/api/sources/gmail/credentials", json={})
    assert resp_empty.status_code == 422

    # 7. Sorgente non implementata -> 400
    resp_unsupported = client.post(
        "/api/sources/immich/credentials",
        json={"raw_json": valid_web_json},
    )
    assert resp_unsupported.status_code == 400


def test_oauth_resolution_and_missing_client_412(tmp_path: Any) -> None:
    # Caso 1: Client non configurato né su DB né su file -> 412 Precondition Failed
    client, _, _ = _build_test_client()
    resp_auth = client.post("/api/sources/gmail/oauth/authorize")
    assert resp_auth.status_code == 412
    assert "richiede il client secret" in resp_auth.json()["detail"]

    # Caso 2: Fallback su secret file montato su filesystem se DB è vuoto
    secret_file = tmp_path / "gmail_client_secret.json"
    secret_file.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "mounted-secret.apps.googleusercontent.com",
                    "client_secret": "mounted-client-secret",
                    "redirect_uris": ["http://localhost:8000/callback"],
                }
            }
        ),
        encoding="utf-8",
    )
    settings_with_file = _test_settings(gmail_oauth_client_secret_file=secret_file)
    fake_auth_session = type("FakeAuthSession", (), {"id": uuid4()})()
    client_fallback, _, _ = _build_test_client(
        settings=settings_with_file,
        auth_session=fake_auth_session,
    )

    resp_auth_file = client_fallback.post("/api/sources/gmail/oauth/authorize")
    assert resp_auth_file.status_code == 200
    auth_data = resp_auth_file.json()
    assert "accounts.google.com" in auth_data["authorization_url"]
    assert "mounted-secret" in auth_data["authorization_url"]


def test_source_connection_live_mocked() -> None:
    client, session, owner = _build_test_client()
    settings = _test_settings()

    # 1. Senza account connesso -> 400
    resp_no_acc = client.post("/api/sources/gmail/test-connection")
    assert resp_no_acc.status_code == 400

    # Creiamo account attivo
    creds = _synthetic_credentials()
    ciphertext = encrypt_credentials(creds, settings)
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="112233",
        primary_email="user@testdomain.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=ciphertext,
        oauth_token_format_version=1,
        confirmed_aliases=["user@testdomain.com"],
    )
    session.add(account)

    # 2. Test connessione con successo (Mock Gateway get_profile)
    with patch("gbrain.presentation.sources_api.OfficialGmailGateway") as mock_gw_cls:
        mock_gateway = mock_gw_cls.from_credentials.return_value
        mock_gateway.get_profile.return_value = {
            "emailAddress": "user@testdomain.com",
            "messagesTotal": 42,
        }

        resp_success = client.post("/api/sources/gmail/test-connection")
        assert resp_success.status_code == 200
        data = resp_success.json()
        assert data["connected"] is True
        assert data["primary_identity"] == "user@testdomain.com"
        assert data["latency_ms"] is not None
        assert "verificata con successo" in data["message"]

    # 3. Test connessione con fallimento remoto API Google
    with patch("gbrain.presentation.sources_api.OfficialGmailGateway") as mock_gw_cls:
        mock_gateway = mock_gw_cls.from_credentials.return_value
        mock_gateway.get_profile.side_effect = RuntimeError("Google API 401 Invalid Token")

        resp_fail = client.post("/api/sources/gmail/test-connection")
        assert resp_fail.status_code == 200
        data_fail = resp_fail.json()
        assert data_fail["connected"] is False
        assert data_fail["latency_ms"] is not None
        assert "non riuscita" in data_fail["message"]

    # 4. Test connessione con token scaduto e refresh proattivo
    from gbrain.application.gmail_oauth import decrypt_credentials as _decrypt

    with (
        patch("gbrain.presentation.sources_api.refresh_credentials") as mock_refresh,
        patch("gbrain.presentation.sources_api.OfficialGmailGateway") as mock_gw_cls,
    ):

        def fake_refresh(creds: Any, setts: Any) -> Any:
            creds.token = "refreshed-live-token"  # noqa: S105
            return creds

        mock_refresh.side_effect = fake_refresh
        mock_gateway = mock_gw_cls.from_credentials.return_value
        mock_gateway.get_profile.return_value = {
            "emailAddress": "user@testdomain.com",
            "messagesTotal": 42,
        }

        # Impostiamo il token nel DB con expiry nel passato
        account.oauth_token_ciphertext = encrypt_credentials(
            Credentials(  # type: ignore[no-untyped-call]
                token="expired-token",  # noqa: S106
                refresh_token="ref-tok",  # noqa: S106
                token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
                client_id="cid",
                client_secret="sec",  # noqa: S106
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                expiry=datetime.now(UTC) - timedelta(hours=2),
            ),
            settings,
        )

        resp_refreshed = client.post("/api/sources/gmail/test-connection")
        assert resp_refreshed.status_code == 200
        data_ref = resp_refreshed.json()
        assert data_ref["connected"] is True
        mock_refresh.assert_called_once()
        # Verifica che il token rinnovato sia stato salvato nel database
        decrypted_after = _decrypt(account, settings)
        assert decrypted_after.token == "refreshed-live-token"  # noqa: S105


def test_source_preview_sent_messages() -> None:
    client, session, owner = _build_test_client()
    settings = _test_settings()

    creds = _synthetic_credentials()
    ciphertext = encrypt_credentials(creds, settings)
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="112233",
        primary_email="user@testdomain.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=ciphertext,
        oauth_token_format_version=1,
        confirmed_aliases=["user@testdomain.com"],
    )
    session.add(account)

    now = datetime.now(UTC)
    mock_raw_msg = GmailRawMessage(
        message_id="msg-101",
        thread_id="thr-101",
        history_id="1",
        label_ids=("SENT",),
        internal_date_ms=int(now.timestamp() * 1000),
        snippet="Preview snippet content",
        payload={
            "headers": [
                {"name": "Subject", "value": "Test Report"},
                {"name": "To", "value": "recipient@partner.com"},
            ],
            "internalDate": str(int(now.timestamp() * 1000)),
        },
        size_estimate=1024,
    )

    with patch("gbrain.presentation.sources_api.OfficialGmailGateway") as mock_gw_cls:
        mock_gateway = mock_gw_cls.from_credentials.return_value
        mock_gateway.list_sent_page.return_value = ConnectorPage(
            items=(GmailLocator(message_id="msg-101", thread_id="thr-101", history_id="1"),),
            next_cursor=None,
            state=PageState.COMPLETE,
        )
        mock_gateway.fetch_sent_message.return_value = mock_raw_msg

        # Default filters
        resp = client.get("/api/sources/gmail/preview?limit=5")
        assert resp.status_code == 200
        preview_data = resp.json()
        assert preview_data["source_id"] == "gmail"
        assert preview_data["estimated_count"] == 1
        assert len(preview_data["sample_items"]) == 1
        item = preview_data["sample_items"][0]
        assert item["id"] == "msg-101"
        assert item["subject"] == "Test Report"
        assert "recipient@partner.com" in item["to"]

        # Date filter non valido (after >= before) -> 400
        bad_after = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bad_before = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        resp_bad_dates = client.get(
            f"/api/sources/gmail/preview?period_after={bad_after}&period_before={bad_before}"
        )
        assert resp_bad_dates.status_code == 400, resp_bad_dates.json()


def test_source_sync_enqueue() -> None:
    procrastinate_mock = AsyncMock()
    configure_task_mock = AsyncMock()
    defer_mock = AsyncMock(return_value=999)
    configure_task_mock.defer_async = defer_mock
    # configure_task è un metodo síncrono su procrastinate.App che ritorna un job configurato
    procrastinate_mock.configure_task = lambda *args, **kwargs: configure_task_mock

    client, session, owner = _build_test_client(procrastinate_mock=procrastinate_mock)
    settings = _test_settings()

    creds = _synthetic_credentials()
    ciphertext = encrypt_credentials(creds, settings)
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="112233",
        primary_email="user@testdomain.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=ciphertext,
        oauth_token_format_version=1,
        confirmed_aliases=["user@testdomain.com"],
    )
    session.add(account)

    # 1. Senza checkpoint valido, la richiesta (anche se richiede incremental)
    # deve essere autoritativamente risolta su "full" dal backend.
    resp_sync = client.post(
        "/api/sources/gmail/sync",
        json={"mode": "incremental", "item_limit": 50},
    )
    assert resp_sync.status_code == 200, resp_sync.json()
    sync_data = resp_sync.json()
    assert sync_data["source_id"] == "gmail"
    assert sync_data["status"] == "pending"
    assert sync_data["mode"] == "full"
    assert sync_data["queue_job_id"] == 999
    assert "avviato" in sync_data["message"]

    # Simula completamento job
    for j in session.jobs.values():
        j.status = ImportJobStatus.COMPLETED

    # 2. Con checkpoint completato con history_id, la richiesta "incremental"
    # viene correttamente accodata in modalità "incremental".
    from gbrain.domain.models import ReconciliationState

    chk = GmailCheckpoint(
        connector_account_id=account.id,
        source_policy_id=uuid4(),
        history_id="123456",
        page_token=None,
        reconciliation_state=ReconciliationState.NOT_REQUIRED,
    )
    session.add(chk)

    resp_sync_inc = client.post(
        "/api/sources/gmail/sync",
        json={"mode": "incremental", "item_limit": 50},
    )
    assert resp_sync_inc.status_code == 200, resp_sync_inc.json()
    sync_data_inc = resp_sync_inc.json()
    assert sync_data_inc["mode"] == "incremental"


def test_source_disconnect() -> None:
    client, session, owner = _build_test_client()
    settings = _test_settings()

    # Creiamo credenziale client nel DB
    raw_payload = {"web": {"client_id": "c1", "client_secret": "s1"}}
    cred = SourceClientCredential(
        owner_id=owner.id,
        source_kind="gmail",
        auth_method="oauth2",
        client_type="web",
        client_id="c1",
        encrypted_credentials=encrypt_json(raw_payload, settings.source_credentials_encryption_key),
    )
    session.add(cred)

    # Creiamo account attivo
    creds = _synthetic_credentials()
    ciphertext = encrypt_credentials(creds, settings)
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="112233",
        primary_email="user@testdomain.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=ciphertext,
        oauth_token_format_version=1,
        confirmed_aliases=["user@testdomain.com"],
    )
    session.add(account)

    # 1. Disconnessione con remove_credentials=False
    with patch("gbrain.application.gmail_oauth.revoke_google_token", return_value=True):
        resp_disc1 = client.delete("/api/sources/gmail?remove_credentials=false")
        assert resp_disc1.status_code == 200
        data1 = resp_disc1.json()
        assert data1["disconnected"] is True
        assert data1["credentials_removed"] is False
        assert len(session.credentials) == 1
        assert account.status == ConnectorAccountStatus.REVOKED
        assert account.oauth_token_ciphertext is None

    # 2. Disconnessione con remove_credentials=True
    resp_disc2 = client.delete("/api/sources/gmail?remove_credentials=true")
    assert resp_disc2.status_code == 200
    data2 = resp_disc2.json()
    assert data2["credentials_removed"] is True
    assert len(session.credentials) == 0


def test_security_unauthenticated_and_csrf_and_no_leak() -> None:
    settings = _test_settings()
    app = FastAPI()
    app.state.settings = settings
    app.include_router(api_router)
    app.dependency_overrides[get_settings] = lambda: settings

    # Per verificare l'autenticazione reale, usiamo una sessione DB fittizia che ritorna None per l'auth session
    class AuthDbSession:
        async def scalar(self, *args: Any, **kwargs: Any) -> Any:
            return None

    async def override_auth_db() -> Any:
        return AuthDbSession()

    app.dependency_overrides[get_db_session] = override_auth_db
    client = TestClient(app)

    # Non autenticato -> 401
    assert client.get("/api/sources/catalog").status_code == 401
    assert client.get("/api/sources/gmail/status").status_code == 401
    assert (
        client.post(
            "/api/sources/gmail/credentials",
            json={"raw_json": "{}"},
        ).status_code
        == 401
    )
    assert client.delete("/api/sources/gmail").status_code == 401

    # Autenticato ma senza header CSRF -> 403 sui metodi mutativi
    test_owner = OwnerAccount(
        id=uuid4(),
        owner_slot=1,
        username="admin",
        display_name="Amministratore",
        password_hash="hash",  # noqa: S106
    )

    async def override_principal() -> Principal:
        return Principal(owner=test_owner, session=None)  # type: ignore[arg-type]

    app.dependency_overrides[require_principal] = override_principal
    # Non sovrascriviamo require_csrf per testare il rifiuto CSRF
    resp_csrf = client.post(
        "/api/sources/gmail/credentials",
        json={"raw_json": "{}"},
    )
    assert resp_csrf.status_code == 403

    # Verifica assenza leak: salvando credenziali con TestClient autenticato,
    # assicurarsi che il secret in chiaro non appaia mai nella risposta JSON
    client_auth, _, _ = _build_test_client()
    raw_cfg = {
        "web": {
            "client_id": "google-client-leak-test.apps.googleusercontent.com",
            "client_secret": "SUPER_SECRET_VALUE_MUST_NOT_LEAK",
        }
    }
    resp_cred = client_auth.post(
        "/api/sources/gmail/credentials",
        json={"raw_json": json.dumps(raw_cfg)},
    )
    assert resp_cred.status_code == 200
    assert "SUPER_SECRET_VALUE_MUST_NOT_LEAK" not in resp_cred.text


def test_source_oauth_callback_flow_and_security() -> None:
    client_auth, _, _ = _build_test_client()

    # 1. Successo: complete_oauth mockata con successo -> redirect verso /sources?connected=gmail&gmail_connected=1
    with patch(
        "gbrain.presentation.sources_api.complete_oauth", new_callable=AsyncMock
    ) as mock_complete:
        resp = client_auth.get(
            "/api/sources/gmail/oauth/callback?code=valid-code&state=valid-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/sources?connected=gmail&gmail_connected=1"
        mock_complete.assert_awaited_once()

    # 2. Errore da Google (es. utente nega il consenso) -> redirect verso /sources?error=access_denied
    resp_err = client_auth.get(
        "/api/sources/gmail/oauth/callback?error=access_denied",
        follow_redirects=False,
    )
    assert resp_err.status_code == 302
    assert resp_err.headers["location"] == "/sources?error=access_denied"

    # 3. Parametri mancanti -> redirect verso /sources?error=...
    resp_missing = client_auth.get(
        "/api/sources/gmail/oauth/callback",
        follow_redirects=False,
    )
    assert resp_missing.status_code == 302
    assert "/sources?error=" in resp_missing.headers["location"]

    # 4. Fallimento di complete_oauth (es. state non valido) -> redirect con messaggio sicuro
    with patch(
        "gbrain.presentation.sources_api.complete_oauth",
        side_effect=GmailOAuthError("invalid_state", "Stato OAuth non valido o scaduto"),
    ):
        resp_oauth_fail = client_auth.get(
            "/api/sources/gmail/oauth/callback?code=bad&state=bad",
            follow_redirects=False,
        )
        assert resp_oauth_fail.status_code == 302
        assert "error=" in resp_oauth_fail.headers["location"]


def test_source_oauth_callback_requires_authenticated_principal() -> None:
    # La callback non deve essere anonima: senza autenticazione deve fallire con 401
    settings = _test_settings()
    app = FastAPI()
    app.state.settings = settings
    app.include_router(api_router)
    app.dependency_overrides[get_settings] = lambda: settings

    class AuthDbSession:
        async def scalar(self, *args: Any, **kwargs: Any) -> Any:
            return None

    async def override_auth_db() -> Any:
        return AuthDbSession()

    app.dependency_overrides[get_db_session] = override_auth_db
    client = TestClient(app)

    resp = client.get(
        "/api/sources/gmail/oauth/callback?code=code&state=state",
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Autenticazione richiesta"}


def test_gmail_api_oauth_callback_flow() -> None:
    from gbrain.presentation.gmail_api import gmail_router

    settings = _test_settings()
    app = FastAPI()
    app.state.settings = settings
    app.include_router(gmail_router)

    test_owner = OwnerAccount(
        id=uuid4(),
        owner_slot=1,
        username="admin",
        display_name="Amministratore",
        password_hash="hash",  # noqa: S106
    )

    async def override_principal() -> Principal:
        return Principal(owner=test_owner, session=None)  # type: ignore[arg-type]

    app.dependency_overrides[require_principal] = override_principal
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = lambda: InMemorySession()
    client = TestClient(app)

    # 1. Successo -> /sources?connected=gmail&gmail_connected=1
    with patch(
        "gbrain.presentation.gmail_api.complete_oauth", new_callable=AsyncMock
    ) as mock_complete:
        resp = client.get(
            "/api/gmail/oauth/callback?code=abc&state=xyz",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/sources?connected=gmail&gmail_connected=1"
        mock_complete.assert_awaited_once()

    # 2. Errore da Google -> /sources?error=consent_denied
    resp_err = client.get(
        "/api/gmail/oauth/callback?error=consent_denied",
        follow_redirects=False,
    )
    assert resp_err.status_code == 303
    assert resp_err.headers["location"] == "/sources?error=consent_denied"


def test_decrypt_credentials_expiry_compatibility() -> None:
    import uuid
    from datetime import UTC, datetime, timedelta

    from google.oauth2.credentials import Credentials

    from gbrain.application.gmail_oauth import (
        decrypt_credentials,
        encrypt_credentials,
    )
    from gbrain.domain.gmail import GMAIL_READONLY_SCOPE
    from gbrain.domain.models import ConnectorAccount, ConnectorAccountStatus

    settings = _test_settings()
    # Creiamo credenziali con expiry aware (es. da serializzazione json UTC)
    future = datetime.now(UTC) + timedelta(hours=1)
    creds = Credentials(  # type: ignore[no-untyped-call]
        token="access_tok",  # noqa: S106
        refresh_token="refresh_tok",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="cid.apps.googleusercontent.com",
        client_secret="secret",  # noqa: S106
        scopes=[GMAIL_READONLY_SCOPE],
        expiry=future,
    )
    ciphertext = encrypt_credentials(creds, settings)
    account = ConnectorAccount(
        owner_id=uuid.uuid4(),
        connector_kind="gmail",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=ciphertext,
        oauth_token_format_version=1,
    )

    restored = decrypt_credentials(account, settings)
    # Verifica fondamentale: l'accesso a valid / expired NON deve sollevare:
    # TypeError: can't compare offset-naive and offset-aware datetimes
    assert restored.valid is True
    assert restored.expired is False
    assert restored.expiry is not None
    assert restored.expiry.tzinfo is None  # Deve essere naive per compatibilità con google.auth


def test_gmail_account_and_alias_confirmation_flow() -> None:
    client, session, owner = _build_test_client()
    settings = _test_settings()

    # Senza account: GET /api/gmail/account -> 404
    resp_no_acc = client.get("/api/gmail/account")
    assert resp_no_acc.status_code == 404

    # Creiamo account attivo senza alias confermati
    creds = _synthetic_credentials()
    ciphertext = encrypt_credentials(creds, settings)
    account = ConnectorAccount(
        id=uuid4(),
        owner_id=owner.id,
        connector_kind="gmail",
        external_account_id="user@testdomain.com",
        primary_email="user@testdomain.com",
        status=ConnectorAccountStatus.ACTIVE,
        oauth_token_ciphertext=ciphertext,
        oauth_token_format_version=1,
        confirmed_aliases=[],
    )
    session.add(account)

    # Verifica stato da /api/sources/gmail/status: aliases_ready è False
    st_resp = client.get("/api/sources/gmail/status")
    assert st_resp.status_code == 200
    st_data = st_resp.json()
    assert st_data["aliases_ready"] is False
    assert st_data["confirmed_aliases"] == []

    # GET /api/gmail/account ora restituisce l'account con aliases_ready False
    acc_resp = client.get("/api/gmail/account")
    assert acc_resp.status_code == 200
    acc_data = acc_resp.json()
    assert acc_data["primary_email"] == "user@testdomain.com"
    assert acc_data["aliases_ready"] is False
    assert acc_data["confirmed_aliases"] == []

    # PUT /api/gmail/aliases con confirmed_addresses (payload flessibile per frontend)
    confirm_resp = client.put(
        "/api/gmail/aliases",
        json={"confirmed_addresses": ["user@testdomain.com"]},
    )
    assert confirm_resp.status_code == 200
    confirmed_data = confirm_resp.json()
    assert confirmed_data["aliases_ready"] is True
    assert confirmed_data["confirmed_aliases"] == ["user@testdomain.com"]

    # Verifica che /api/sources/gmail/status rifletta aliases_ready True
    st_resp2 = client.get("/api/sources/gmail/status")
    assert st_resp2.status_code == 200
    st_data2 = st_resp2.json()
    assert st_data2["aliases_ready"] is True
    assert st_data2["confirmed_aliases"] == ["user@testdomain.com"]
