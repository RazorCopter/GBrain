from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Response
from pydantic import SecretStr
from sqlalchemy import UniqueConstraint

from gbrain.application.auth import (
    _new_session,
    _password_hasher,
    _session_model,
    logout,
)
from gbrain.config import Settings
from gbrain.domain.models import OutboxEvent, OwnerAccount
from gbrain.logging import JsonFormatter, SensitiveDataFilter
from gbrain.presentation.api import _clear_session_cookies, _set_session_cookies
from gbrain.presentation.health import live, ready


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "postgres_password": SecretStr("pg-secret"),
        "auth_secret": SecretStr("a" * 40),
        "neo4j_password": SecretStr("neo-secret"),
    }
    values.update(overrides)
    return Settings(**values)


def test_password_hash_is_argon2_and_verifiable_without_plaintext() -> None:
    password = "password-sintetica-molto-lunga"  # noqa: S105 - credenziale fixture
    password_hash = _password_hasher.hash(password)

    assert password not in password_hash
    assert password_hash.startswith("$argon2id$")
    assert _password_hasher.verify(password_hash, password)


def test_session_cookies_have_expected_security_attributes() -> None:
    settings = _settings(session_secure_cookie=True)
    response = Response()

    _set_session_cookies(response, settings, "session-token", "csrf-token")
    cookies = response.headers.getlist("set-cookie")

    session_cookie = next(value for value in cookies if value.startswith("gbrain_session="))
    csrf_cookie = next(value for value in cookies if value.startswith("gbrain_csrf="))
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=strict" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" in csrf_cookie
    assert "SameSite=strict" in csrf_cookie


def test_clear_session_cookies_invalidates_both_cookies() -> None:
    response = Response()

    _clear_session_cookies(response, _settings())
    cookies = response.headers.getlist("set-cookie")

    assert len(cookies) == 2
    assert all("Max-Age=0" in value for value in cookies)
    assert any(value.startswith("gbrain_session=") for value in cookies)
    assert any(value.startswith("gbrain_csrf=") for value in cookies)


class _CommitSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


async def test_logout_revokes_server_side_session() -> None:
    settings = _settings()
    issued = _new_session(uuid.uuid4(), settings)
    auth_session = _session_model(uuid.uuid4(), issued, settings)
    database_session = _CommitSession()

    await logout(cast(Any, database_session), auth_session)

    assert auth_session.revoked_at is not None
    assert auth_session.revoked_at <= datetime.now(UTC)
    assert database_session.commits == 1


class _Probe:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def ping(self) -> None:
        if self.error is not None:
            raise self.error


@pytest.mark.parametrize(
    ("postgres_error", "neo4j_error", "expected_code", "expected_status", "expected_neo4j"),
    [
        (None, None, 200, "ok", "ok"),
        (None, RuntimeError("neo down"), 200, "ok", "unavailable"),
        (RuntimeError("pg down"), None, 503, "unavailable", "ok"),
    ],
)
async def test_health_readiness_semantics(
    postgres_error: Exception | None,
    neo4j_error: Exception | None,
    expected_code: int,
    expected_status: str,
    expected_neo4j: str,
) -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                database=_Probe(postgres_error),
                graph=_Probe(neo4j_error),
            )
        )
    )
    response = Response()

    payload = await ready(cast(Any, request), response)

    assert response.status_code == expected_code
    assert payload["status"] == expected_status
    assert payload["neo4j_projection"] == expected_neo4j
    assert payload["graphiti"] == "not_implemented"


async def test_liveness_has_no_dependency_probe() -> None:
    assert await live() == {"status": "ok"}


def test_single_owner_and_outbox_minimum_constraints() -> None:
    owner_table = cast(Any, OwnerAccount.__table__)
    outbox_table = cast(Any, OutboxEvent.__table__)
    owner_constraints = {
        constraint.name for constraint in owner_table.constraints if constraint.name is not None
    }
    outbox_constraints = {
        constraint.name
        for constraint in outbox_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert "ck_single_owner_slot" in owner_constraints
    assert "uq_outbox_stable_key" in outbox_constraints
    assert outbox_table.c.id.type.as_uuid is True
    assert outbox_table.c.aggregate_id.type.as_uuid is True
    assert outbox_table.c.stable_key.type.length == 200


def test_structured_log_filter_redacts_sensitive_dictionary_values() -> None:
    record = logging.LogRecord(
        name="gbrain.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg={"password": "non-deve-apparire", "event": "login"},
        args=(),
        exc_info=None,
    )

    assert SensitiveDataFilter().filter(record)
    rendered = JsonFormatter().format(record)
    payload = json.loads(rendered)

    assert "non-deve-apparire" not in rendered
    assert "[REDACTED]" in payload["message"]
    assert "login" in payload["message"]


def test_session_expiry_is_deterministic_with_configured_ttl() -> None:
    before = datetime.now(UTC) + timedelta(seconds=599)
    issued = _new_session(uuid.uuid4(), _settings(session_ttl_seconds=600))
    after = datetime.now(UTC) + timedelta(seconds=601)

    assert before <= issued.expires_at <= after
