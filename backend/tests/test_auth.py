from __future__ import annotations

import uuid

from pydantic import SecretStr

from gbrain.application.auth import _new_session, _session_model, validate_csrf
from gbrain.config import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        postgres_password=SecretStr("pg-secret"),
        auth_secret=SecretStr("a" * 40),
        neo4j_password=SecretStr("neo-secret"),
    )


def test_csrf_token_is_hashed_and_validated_with_double_submit_cookie() -> None:
    settings = _settings()
    issued = _new_session(uuid.uuid4(), settings)
    session = _session_model(uuid.uuid4(), issued, settings)

    assert issued.csrf_token.encode() not in session.csrf_hash
    assert validate_csrf(settings, session, issued.csrf_token, issued.csrf_token)
    assert not validate_csrf(settings, session, "token-sbagliato", issued.csrf_token)
    assert not validate_csrf(settings, session, issued.csrf_token, "cookie-sbagliato")
    assert not validate_csrf(settings, session, None, issued.csrf_token)
    assert not validate_csrf(settings, session, issued.csrf_token, None)


def test_session_token_is_not_stored_in_plaintext() -> None:
    settings = _settings()
    owner_id = uuid.uuid4()
    issued = _new_session(owner_id, settings)
    session = _session_model(owner_id, issued, settings)

    assert len(session.token_hash) == 32
    assert issued.raw_token.encode() not in session.token_hash
