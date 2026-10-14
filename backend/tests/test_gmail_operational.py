from __future__ import annotations

import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from pydantic import SecretStr, ValidationError

from gbrain.application.gmail_oauth import (
    GmailOAuthError,
    OAuthIdentity,
    decrypt_secret,
    deserialize_credentials,
    encrypt_credentials,
    encrypt_secret,
    serialize_credentials,
)
from gbrain.application.gmail_service import (
    GmailServiceError,
    PolicyInput,
    normalize_aliases,
)
from gbrain.config import Settings
from gbrain.domain.gmail import GMAIL_READONLY_SCOPE
from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway
from gbrain.presentation.dependencies import get_db_session, require_csrf, require_principal
from gbrain.presentation.gmail_api import gmail_router


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
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
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "postgres_password": SecretStr("pg-secret"),
        "auth_secret": SecretStr("a" * 40),
        "neo4j_password": SecretStr("neo-secret"),
        "gmail_oauth_client_secret_file": client,
        "gmail_token_encryption_key_file": key,
    }
    values.update(overrides)
    return Settings(**values)


def _credentials(scopes: list[str] | None = None) -> Credentials:
    return Credentials(  # type: ignore[no-untyped-call]
        token="synthetic-access-token",  # noqa: S106 - fixture non reale
        refresh_token="synthetic-refresh-token",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106 - endpoint, non password
        client_id="synthetic.apps.example.invalid",
        client_secret="synthetic-client-secret",  # noqa: S106
        scopes=scopes or [GMAIL_READONLY_SCOPE],
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )


def test_token_encryption_is_authenticated_versioned_and_redacted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    credentials = _credentials()
    ciphertext = encrypt_credentials(credentials, settings)

    assert ciphertext[0] == 1
    assert b"synthetic-access-token" not in ciphertext
    stored = deserialize_credentials(
        decrypt_secret(
            ciphertext, settings.gmail_token_encryption_key, context=b"gbrain:gmail-oauth-token:v1"
        )
    )
    assert stored.scopes == (GMAIL_READONLY_SCOPE,)
    assert stored.token == credentials.token

    tampered = bytearray(ciphertext)
    tampered[-1] ^= 1
    with pytest.raises(GmailOAuthError, match="Integrità"):
        decrypt_secret(
            bytes(tampered),
            settings.gmail_token_encryption_key,
            context=b"gbrain:gmail-oauth-token:v1",
        )


def test_scope_must_be_exactly_readonly(tmp_path: Path) -> None:
    _settings(tmp_path)
    with pytest.raises(GmailOAuthError, match="esclusivamente"):
        serialize_credentials(
            _credentials([GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.modify"])
        )


def test_production_requires_coherent_https_redirect(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        _settings(tmp_path, environment="production", public_url="http://example.invalid")
    with pytest.raises(ValidationError, match="coincidere"):
        _settings(
            tmp_path,
            public_url="https://brain.example.invalid",
            gmail_oauth_redirect_uri="https://other.example.invalid/callback",
        )


def test_alias_confirmation_only_accepts_api_verified_identity() -> None:
    assert normalize_aliases(["Owner@Example.test"], ["owner@example.test"]) == [
        "owner@example.test"
    ]
    with pytest.raises(GmailServiceError, match="verificato"):
        normalize_aliases(["from-header@example.test"], ["owner@example.test"])
    with pytest.raises(GmailServiceError, match="almeno un alias"):
        normalize_aliases([], ["owner@example.test"])


def test_policy_snapshot_has_sent_perimeter_and_no_query() -> None:
    value = PolicyInput(
        after=datetime(2024, 1, 1, tzinfo=UTC),
        before=datetime(2024, 2, 1, tzinfo=UTC),
        item_limit=100,
        request_budget=300,
    )
    snapshot = value.snapshot()
    assert snapshot["perimeter"] == "SENT"
    assert snapshot["scope"] == GMAIL_READONLY_SCOPE
    assert "query" not in snapshot


def test_private_gmail_routes_require_auth_and_mutations_require_csrf() -> None:
    by_path = {route.path: route for route in gmail_router.routes if isinstance(route, APIRoute)}
    private_gets = [
        "/api/gmail/status",
        "/api/gmail/sources",
        "/api/gmail/search",
        "/api/gmail/jobs/{job_id}",
        "/api/gmail/sources/{source_item_id}/proof",
        "/api/gmail/sources/{source_item_id}/live-evidence",
    ]
    for path in private_gets:
        calls = {dependency.call for dependency in by_path[path].dependant.dependencies}
        assert require_principal in calls
    mutations = [
        "/api/gmail/oauth/start",
        "/api/gmail/disconnect",
        "/api/gmail/aliases",
        "/api/gmail/aliases/{alias}",
        "/api/gmail/policy",
        "/api/gmail/preview",
        "/api/gmail/jobs",
        "/api/gmail/jobs/{job_id}/cancel",
    ]
    for path in mutations:
        calls = {dependency.call for dependency in by_path[path].dependant.dependencies}
        assert require_csrf in calls


def test_gmail_api_rejects_anonymous_and_missing_csrf(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = FastAPI()
    app.state.settings = settings
    app.include_router(gmail_router)

    async def no_database() -> Any:
        yield None

    app.dependency_overrides[get_db_session] = no_database
    with TestClient(app) as client:
        assert client.get("/api/gmail/status").status_code == 401

        owner_id = uuid.uuid4()
        from gbrain.application.auth import _new_session, _session_model
        from gbrain.domain.models import OwnerAccount
        from gbrain.presentation.dependencies import Principal

        issued = _new_session(owner_id, settings)
        principal = Principal(
            owner=OwnerAccount(
                id=owner_id,
                owner_slot=1,
                username="owner-gmail",
                display_name="Owner Gmail Sintetico",
                password_hash="unused-synthetic-hash",  # noqa: S106
            ),
            session=_session_model(owner_id, issued, settings),
        )

        async def authenticated() -> Principal:
            return principal

        app.dependency_overrides[require_principal] = authenticated
        assert client.post("/api/gmail/oauth/start").status_code == 403
        client.cookies.set("gbrain_csrf", issued.csrf_token)
        assert (
            client.post(
                "/api/gmail/oauth/start",
                headers={"X-CSRF-Token": "csrf-diverso"},
            ).status_code
            == 403
        )


def test_worker_task_payload_has_no_token_and_gateway_has_no_write_methods() -> None:
    from gbrain.application.gmail_jobs import execute_gmail_job

    signature = inspect.signature(execute_gmail_job)
    assert {"job_id", "account_id", "mode"} <= set(signature.parameters)
    worker_source = (Path(__file__).parents[1] / "src" / "gbrain" / "worker.py").read_text(
        encoding="utf-8"
    )
    assert 'lock="gmail-heavy-global"' in worker_source
    assert "access_token" not in worker_source
    assert "refresh_token" not in worker_source
    assert not hasattr(OfficialGmailGateway, "send")
    assert not hasattr(OfficialGmailGateway, "modify")
    assert not hasattr(OfficialGmailGateway, "drafts")


def test_oauth_identity_is_explicit_not_from_header() -> None:
    identity = OAuthIdentity(
        external_account_id="owner@example.test",
        primary_email="owner@example.test",
        available_aliases=("owner@example.test",),
    )
    assert identity.available_aliases == (identity.primary_email,)
    assert not hasattr(identity, "from_header")


def test_encrypt_secret_uses_context_binding(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    encrypted = encrypt_secret(b"synthetic", settings.gmail_token_encryption_key, context=b"one")
    with pytest.raises(GmailOAuthError):
        decrypt_secret(encrypted, settings.gmail_token_encryption_key, context=b"two")


def test_no_oauth_secret_fields_are_in_public_schemas() -> None:
    from gbrain.presentation.schemas import GmailAccountResponse, GmailStatusResponse

    fields = set(GmailAccountResponse.model_fields) | set(GmailStatusResponse.model_fields)
    assert not fields & {"token", "access_token", "refresh_token", "client_secret", "state"}


def test_job_task_is_globally_locked_and_bounded() -> None:
    worker_source = (Path(__file__).parents[1] / "src" / "gbrain" / "worker.py").read_text(
        encoding="utf-8"
    )
    assert 'lock="gmail-heavy-global"' in worker_source
    assert 'queue="gmail"' in worker_source
    assert "retry=GmailJobRetryStrategy(" in worker_source
    assert "retry_exceptions=(GmailJobTransientError,)" in worker_source


def test_synthetic_credentials_repr_is_not_used_as_api_payload() -> None:
    payload = {"job_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "mode": "full"}
    encoded = json.dumps(payload)
    assert "token" not in encoded.casefold()
    assert set(payload) == {"job_id", "account_id", "mode"}
