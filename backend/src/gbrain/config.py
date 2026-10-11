from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote_plus

from pydantic import BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(ValueError):
    """Errore di configurazione sicuro da mostrare senza includere segreti."""


def _empty_to_none(value: object) -> object:
    if value == "":
        return None
    return value


OptionalSecret = Annotated[SecretStr | None, BeforeValidator(_empty_to_none)]
OptionalPath = Annotated[Path | None, BeforeValidator(_empty_to_none)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GBRAIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    web_host: str = "0.0.0.0"  # noqa: S104 - necessario nel container
    web_port: int = Field(default=8000, ge=1, le=65535)
    public_url: str = "http://localhost:8000"
    demo_mode: bool = Field(
        default=False,
        description="Abilita esclusivamente fixture sintetiche; non modifica i controlli di sicurezza.",
    )

    data_dir: Path = Path("./runtime/data")
    cache_dir: Path = Path("./runtime/cache")
    import_dir: Path = Path("./runtime/import")
    backup_dir: Path = Path("./runtime/backup")
    frontend_dir: Path = Path("./frontend/dist")

    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "gbrain"
    postgres_user: str = "gbrain"
    postgres_password: OptionalSecret = None
    postgres_password_file: OptionalPath = None

    auth_secret: OptionalSecret = None
    auth_secret_file: OptionalPath = None
    session_cookie_name: str = "gbrain_session"
    session_ttl_seconds: int = Field(default=43_200, ge=300, le=2_592_000)
    session_secure_cookie: bool = False

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: OptionalSecret = None
    neo4j_password_file: OptionalPath = None
    neo4j_database: str = "neo4j"

    worker_concurrency: int = Field(default=1, ge=1, le=1)

    @staticmethod
    def _read_secret_file(path: Path, label: str) -> SecretStr:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"Impossibile leggere il secret file per {label}") from exc
        if not value:
            raise ConfigurationError(f"Il secret file per {label} è vuoto")
        return SecretStr(value)

    @model_validator(mode="after")
    def resolve_and_validate_secrets(self) -> Settings:
        pairs = (
            ("postgres_password", "postgres_password_file", "PostgreSQL"),
            ("auth_secret", "auth_secret_file", "autenticazione"),
            ("neo4j_password", "neo4j_password_file", "Neo4j"),
        )
        for value_name, file_name, label in pairs:
            value = getattr(self, value_name)
            path = getattr(self, file_name)
            if value is not None and path is not None:
                raise ConfigurationError(
                    f"Configurare un solo valore tra variabile e secret file per {label}"
                )
            if path is not None:
                setattr(self, value_name, self._read_secret_file(path, label))

        missing = [label for value_name, _, label in pairs if getattr(self, value_name) is None]
        if missing:
            raise ConfigurationError(
                "Configurazione incompleta: secret obbligatori mancanti per " + ", ".join(missing)
            )

        if self.demo_mode and self.environment == "production":
            raise ConfigurationError("GBRAIN_DEMO_MODE non può essere abilitata in production")
        if self.environment == "production" and not self.public_url.startswith("https://"):
            raise ConfigurationError("In produzione GBRAIN_PUBLIC_URL deve usare HTTPS")

        auth_secret = self.require_secret(self.auth_secret, "autenticazione")
        if len(auth_secret) < 32:
            raise ConfigurationError(
                "Il secret di autenticazione deve contenere almeno 32 caratteri"
            )
        if self.environment == "production" and not self.session_secure_cookie:
            raise ConfigurationError("In produzione GBRAIN_SESSION_SECURE_COOKIE deve essere true")
        return self

    @staticmethod
    def require_secret(secret: SecretStr | None, label: str) -> str:
        if secret is None:  # pragma: no cover - garantito dal validatore
            raise ConfigurationError(f"Secret mancante per {label}")
        return secret.get_secret_value()

    @property
    def database_url(self) -> str:
        password = quote_plus(self.require_secret(self.postgres_password, "PostgreSQL"))
        user = quote_plus(self.postgres_user)
        return (
            f"postgresql+psycopg://{user}:{password}@{self.postgres_host}:"
            f"{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def procrastinate_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    @property
    def auth_signing_secret(self) -> str:
        return self.require_secret(self.auth_secret, "autenticazione")

    @property
    def neo4j_secret(self) -> str:
        return self.require_secret(self.neo4j_password, "Neo4j")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
