from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from gbrain.config import Settings


def test_settings_read_secret_files(tmp_path: Path) -> None:
    postgres = tmp_path / "postgres"
    auth = tmp_path / "auth"
    neo4j = tmp_path / "neo4j"
    postgres.write_text("pg-secret\n", encoding="utf-8")
    auth.write_text("a" * 40, encoding="utf-8")
    neo4j.write_text("neo-secret\n", encoding="utf-8")

    settings = Settings(
        _env_file=None,
        postgres_password_file=postgres,
        auth_secret_file=auth,
        neo4j_password_file=neo4j,
    )

    assert "pg-secret" in settings.database_url
    assert settings.auth_signing_secret == "a" * 40
    assert len(settings.neo4j_secret) == 10


def test_settings_reject_short_auth_secret() -> None:
    with pytest.raises(ValidationError, match="almeno 32"):
        Settings(
            _env_file=None,
            postgres_password=SecretStr("pg-secret"),
            auth_secret=SecretStr("short"),
            neo4j_password=SecretStr("neo-secret"),
        )


def test_settings_rejects_duplicate_secret_sources(tmp_path: Path) -> None:
    secret_file = tmp_path / "postgres"
    secret_file.write_text("file-secret", encoding="utf-8")
    with pytest.raises(ValidationError, match="un solo valore"):
        Settings(
            _env_file=None,
            postgres_password=SecretStr("inline-secret"),
            postgres_password_file=secret_file,
            auth_secret=SecretStr("a" * 40),
            neo4j_password=SecretStr("neo-secret"),
        )


def test_gmail_token_key_requires_separate_32_byte_hex_secret_file(tmp_path: Path) -> None:
    key_file = tmp_path / "gmail-token-key"
    key_file.write_text("ab" * 32, encoding="utf-8")
    settings = Settings(
        _env_file=None,
        postgres_password=SecretStr("pg-secret"),
        auth_secret=SecretStr("a" * 40),
        neo4j_password=SecretStr("neo-secret"),
        gmail_token_encryption_key_file=key_file,
    )

    assert settings.gmail_token_encryption_key == bytes.fromhex("ab" * 32)

    key_file.write_text("troppo-corta", encoding="utf-8")
    with pytest.raises(ValueError, match="esadecimale"):
        _ = settings.gmail_token_encryption_key


def test_production_requires_secure_cookie() -> None:
    with pytest.raises(ValidationError, match="SECURE_COOKIE"):
        Settings(
            _env_file=None,
            environment="production",
            public_url="https://gbrain.example.test",
            postgres_password=SecretStr("pg-secret"),
            auth_secret=SecretStr("a" * 40),
            neo4j_password=SecretStr("neo-secret"),
            session_secure_cookie=False,
        )


def test_production_requires_https_public_url() -> None:
    with pytest.raises(ValidationError, match=r"PUBLIC_URL.*HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            public_url="http://gbrain.example.test",
            postgres_password=SecretStr("pg-secret"),
            auth_secret=SecretStr("a" * 40),
            neo4j_password=SecretStr("neo-secret"),
            session_secure_cookie=True,
        )


def test_session_samesite_cookie_defaults_to_lax() -> None:
    settings = Settings(
        _env_file=None,
        postgres_password=SecretStr("pg-secret"),
        auth_secret=SecretStr("a" * 40),
        neo4j_password=SecretStr("neo-secret"),
    )

    assert settings.session_samesite_cookie == "lax"


def test_demo_mode_defaults_to_off() -> None:
    settings = Settings(
        _env_file=None,
        postgres_password=SecretStr("pg-secret"),
        auth_secret=SecretStr("a" * 40),
        neo4j_password=SecretStr("neo-secret"),
    )

    assert settings.demo_mode is False


def test_demo_mode_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="DEMO_MODE"):
        Settings(
            _env_file=None,
            environment="production",
            public_url="https://gbrain.example.test",
            demo_mode=True,
            postgres_password=SecretStr("pg-secret"),
            auth_secret=SecretStr("a" * 40),
            neo4j_password=SecretStr("neo-secret"),
            session_secure_cookie=True,
        )
