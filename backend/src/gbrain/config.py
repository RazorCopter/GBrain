from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote_plus, urlsplit

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
    session_samesite_cookie: Literal["lax", "strict", "none"] = "lax"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: OptionalSecret = None
    neo4j_password_file: OptionalPath = None
    neo4j_database: str = "neo4j"

    # I secret OAuth sono accettati esclusivamente tramite file montati. Il
    # setup Gmail può restare assente senza impedire l'avvio dell'applicazione.
    gmail_oauth_client_secret_file: OptionalPath = None
    gmail_token_encryption_key_file: OptionalPath = None
    source_credentials_encryption_key_file: OptionalPath = None
    gmail_oauth_redirect_uri: str | None = None
    gmail_oauth_state_ttl_seconds: int = Field(default=600, ge=300, le=900)
    gmail_oauth_timeout_seconds: float = Field(default=20.0, ge=1.0, le=60.0)
    gmail_job_retry_budget: int = Field(default=3, ge=0, le=5)

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
        public = urlsplit(self.public_url)
        if (
            public.scheme not in {"http", "https"}
            or not public.netloc
            or public.query
            or public.fragment
        ):
            raise ConfigurationError("GBRAIN_PUBLIC_URL deve essere un URL HTTP(S) assoluto")
        if self.environment == "production" and public.scheme != "https":
            raise ConfigurationError("In produzione GBRAIN_PUBLIC_URL deve usare HTTPS")

        expected_redirect = f"{self.public_url.rstrip('/')}/api/gmail/oauth/callback"
        if self.gmail_oauth_redirect_uri is None:
            self.gmail_oauth_redirect_uri = expected_redirect
        elif self.gmail_oauth_redirect_uri != expected_redirect:
            raise ConfigurationError(
                "GBRAIN_GMAIL_OAUTH_REDIRECT_URI deve coincidere con la callback del PUBLIC_URL"
            )
        redirect = urlsplit(self.gmail_oauth_redirect_uri)
        if redirect.scheme not in {"http", "https"} or not redirect.netloc:
            raise ConfigurationError("La redirect URI Gmail deve essere un URL HTTP(S) assoluto")
        if self.environment == "production" and redirect.scheme != "https":
            raise ConfigurationError("In produzione la redirect URI Gmail deve usare HTTPS")

        for path, label in (
            (self.gmail_oauth_client_secret_file, "client OAuth Gmail"),
            (self.gmail_token_encryption_key_file, "cifratura token Gmail"),
            (self.source_credentials_encryption_key_file, "cifratura credenziali fonti"),
        ):
            if path is not None:
                self._read_secret_file(path, label)

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
    def gmail_token_encryption_key(self) -> bytes:
        path = self.gmail_token_encryption_key_file
        if path is None:
            raise ConfigurationError(
                "Gmail token encryption key non configurata: usare un secret file separato"
            )
        value = self._read_secret_file(path, "cifratura token Gmail").get_secret_value()
        try:
            decoded = bytes.fromhex(value)
        except ValueError as exc:
            raise ConfigurationError(
                "La chiave di cifratura token Gmail deve essere esadecimale"
            ) from exc
        if len(decoded) != 32:
            raise ConfigurationError("La chiave di cifratura token Gmail deve contenere 32 byte")
        return decoded

    @property
    def source_credentials_encryption_key(self) -> bytes:
        from gbrain.application.crypto import derive_credentials_key

        if self.source_credentials_encryption_key_file is not None:
            raw = self._read_secret_file(
                self.source_credentials_encryption_key_file,
                "cifratura credenziali fonti",
            ).get_secret_value()
            try:
                decoded = bytes.fromhex(raw)
            except ValueError as exc:
                raise ConfigurationError(
                    "La chiave di cifratura credenziali fonti deve essere esadecimale"
                ) from exc
            if len(decoded) != 32:
                raise ConfigurationError(
                    "La chiave di cifratura credenziali fonti deve contenere 32 byte"
                )
            return decoded
        return derive_credentials_key(self.auth_signing_secret)

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
    def gmail_oauth_configured(self) -> bool:
        return (
            self.gmail_oauth_client_secret_file is not None
            and self.gmail_token_encryption_key_file is not None
        )

    @property
    def neo4j_secret(self) -> str:
        return self.require_secret(self.neo4j_password, "Neo4j")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
