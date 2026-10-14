from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.config import ConfigurationError, Settings
from gbrain.domain.gmail import GMAIL_READONLY_SCOPE, normalize_email_address
from gbrain.domain.models import (
    AuthSession,
    ConnectorAccount,
    ConnectorAccountStatus,
    GmailOAuthState,
    OwnerAccount,
)
from gbrain.infrastructure.gmail_gateway import (
    _sanitize_error_text,
    is_transient_transport_error,
)
from gbrain.infrastructure.gmail_rate_limiter import (
    GMAIL_OAUTH_PRE_ACCOUNT_SCOPE,
    GmailRateLimiter,
    PostgresGmailRateLimiter,
)

_TOKEN_FORMAT_VERSION = 1
_TOKEN_ASSOCIATED_DATA = b"gbrain:gmail-oauth-token:v1"
_STATE_ASSOCIATED_DATA = b"gbrain:gmail-oauth-state:v1"
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class GmailOAuthError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class GmailOAuthTransientError(GmailOAuthError):
    """Errore temporaneo di trasporto, rete, DNS, timeout o TLS durante le operazioni OAuth."""


class GmailOAuthNotConfiguredError(GmailOAuthError):
    def __init__(self) -> None:
        super().__init__(
            "oauth_not_configured",
            "OAuth Gmail richiede il client secret e la chiave token montati come secret file.",
        )


class GmailOAuthStateError(GmailOAuthError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthStart:
    authorization_url: str
    state: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StoredToken:
    token: str
    refresh_token: str | None
    token_uri: str
    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
    expiry: datetime | None


@dataclass(frozen=True, slots=True)
class OAuthIdentity:
    external_account_id: str
    primary_email: str
    available_aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RevocationResult:
    attempted: bool
    remote_revoked: bool
    detail_code: str


class OAuthFlowLike(Protocol):
    credentials: Credentials

    def authorization_url(self, **kwargs: object) -> tuple[str, str]: ...

    def fetch_token(self, **kwargs: object) -> None: ...


FlowFactory = Callable[[str], OAuthFlowLike]
IdentityResolver = Callable[..., OAuthIdentity]
Revoker = Callable[[str], bool]


def _urlsafe_digest(value: str, secret: str) -> bytes:
    return hmac.digest(secret.encode("utf-8"), value.encode("utf-8"), "sha256")


def _derive_encryption_key(secret: bytes) -> bytes:
    if len(secret) != 32:
        raise ConfigurationError("La chiave di cifratura Gmail deve contenere 32 byte")
    return secret


def encrypt_secret(plaintext: bytes, key_secret: bytes, *, context: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_derive_encryption_key(key_secret)).encrypt(nonce, plaintext, context)
    return bytes([_TOKEN_FORMAT_VERSION]) + nonce + ciphertext


def decrypt_secret(ciphertext: bytes, key_secret: bytes, *, context: bytes) -> bytes:
    if len(ciphertext) < 30 or ciphertext[0] != _TOKEN_FORMAT_VERSION:
        raise GmailOAuthError("token_format_invalid", "Formato delle credenziali Gmail non valido")
    try:
        return AESGCM(_derive_encryption_key(key_secret)).decrypt(
            ciphertext[1:13], ciphertext[13:], context
        )
    except InvalidTag as exc:
        raise GmailOAuthError(
            "token_authentication_failed", "Integrità delle credenziali Gmail non verificata"
        ) from exc


def serialize_credentials(credentials: Credentials) -> bytes:
    scopes = frozenset(credentials.scopes or credentials.granted_scopes or ())
    if scopes != frozenset({GMAIL_READONLY_SCOPE}):
        raise GmailOAuthError(
            "scope_mismatch", "Google deve concedere esclusivamente lo scope gmail.readonly"
        )
    if not credentials.token or not credentials.refresh_token:
        raise GmailOAuthError(
            "offline_token_missing",
            "Google non ha restituito un refresh token; revocare il consenso e riprovare.",
        )
    payload = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": sorted(scopes),
        "expiry": (
            (
                credentials.expiry.replace(tzinfo=UTC).isoformat()
                if credentials.expiry.tzinfo is None
                else credentials.expiry.astimezone(UTC).isoformat()
            )
            if credentials.expiry is not None
            else None
        ),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def deserialize_credentials(payload: bytes) -> StoredToken:
    try:
        value = json.loads(payload)
        scopes = tuple(str(scope) for scope in value["scopes"])
        expiry_value = value.get("expiry")
        parsed_expiry: datetime | None = None
        if expiry_value:
            parsed_expiry = datetime.fromisoformat(str(expiry_value))
            if parsed_expiry.tzinfo is not None:
                parsed_expiry = parsed_expiry.astimezone(UTC).replace(tzinfo=None)
        result = StoredToken(
            token=str(value["token"]),
            refresh_token=(str(value["refresh_token"]) if value.get("refresh_token") else None),
            token_uri=str(value["token_uri"]),
            client_id=str(value.get("client_id") or ""),
            client_secret=str(value.get("client_secret") or ""),
            scopes=scopes,
            expiry=parsed_expiry,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GmailOAuthError(
            "token_payload_invalid", "Credenziali Gmail memorizzate non valide"
        ) from exc
    if frozenset(result.scopes) != frozenset({GMAIL_READONLY_SCOPE}):
        raise GmailOAuthError(
            "scope_mismatch", "Le credenziali Gmail non hanno lo scope readonly esatto"
        )
    return result


def credentials_from_stored(stored: StoredToken) -> Credentials:
    expiry = stored.expiry
    if expiry is not None and expiry.tzinfo is not None:
        expiry = expiry.astimezone(UTC).replace(tzinfo=None)
    return Credentials(  # type: ignore[no-untyped-call]
        token=stored.token,
        refresh_token=stored.refresh_token,
        token_uri=stored.token_uri,
        client_id=stored.client_id or None,
        client_secret=stored.client_secret or None,
        scopes=list(stored.scopes),
        expiry=expiry,
    )


def _get_token_encryption_key(settings: Settings) -> bytes:
    if settings.gmail_token_encryption_key_file is not None:
        return settings.gmail_token_encryption_key
    return settings.source_credentials_encryption_key


def encrypt_credentials(credentials: Credentials, settings: Settings) -> bytes:
    return encrypt_secret(
        serialize_credentials(credentials),
        _get_token_encryption_key(settings),
        context=_TOKEN_ASSOCIATED_DATA,
    )


def decrypt_credentials(account: ConnectorAccount, settings: Settings) -> Credentials:
    if (
        account.oauth_token_ciphertext is None
        or account.oauth_token_format_version != _TOKEN_FORMAT_VERSION
    ):
        raise GmailOAuthError("credentials_missing", "Credenziali Gmail non disponibili")
    try:
        raw_token = decrypt_secret(
            account.oauth_token_ciphertext,
            _get_token_encryption_key(settings),
            context=_TOKEN_ASSOCIATED_DATA,
        )
    except GmailOAuthError:
        # Fallback alla chiave secondaria derivata se fallisce con la chiave montata
        raw_token = decrypt_secret(
            account.oauth_token_ciphertext,
            settings.source_credentials_encryption_key,
            context=_TOKEN_ASSOCIATED_DATA,
        )
    stored = deserialize_credentials(raw_token)
    return credentials_from_stored(stored)


def refresh_credentials(credentials: Credentials, settings: Settings) -> Credentials:
    scopes = frozenset(credentials.scopes or ())
    if scopes != frozenset({GMAIL_READONLY_SCOPE}):
        raise GmailOAuthError("scope_mismatch", "Refresh Gmail rifiutato: scope non esatto")
    if credentials.expiry is not None and credentials.expiry.tzinfo is not None:
        credentials.expiry = credentials.expiry.astimezone(UTC).replace(tzinfo=None)
    try:
        credentials.refresh(GoogleAuthRequest())  # type: ignore[no-untyped-call]
    except Exception as exc:
        sanitized = _sanitize_error_text(str(exc))
        detail = f": {sanitized}" if sanitized else ""
        if is_transient_transport_error(exc):
            raise GmailOAuthTransientError(
                "oauth_transport_error",
                f"Rinnovo autorizzazione Gmail temporaneamente non riuscito per errore di trasporto{detail}",
            ) from exc
        code = "invalid_grant" if "invalid_grant" in str(exc).lower() else "refresh_failed"
        raise GmailOAuthError(code, f"Rinnovo autorizzazione Gmail non riuscito{detail}") from exc
    if credentials.expiry is not None and credentials.expiry.tzinfo is not None:
        credentials.expiry = credentials.expiry.astimezone(UTC).replace(tzinfo=None)
    refreshed_scopes = frozenset(credentials.scopes or credentials.granted_scopes or ())
    if refreshed_scopes != frozenset({GMAIL_READONLY_SCOPE}):
        raise GmailOAuthError("scope_mismatch", "Refresh Gmail ha restituito scope non valido")
    return credentials


@dataclass(frozen=True, slots=True)
class ResolvedOAuthClient:
    client_config: dict[str, Any]
    redirect_uri: str
    auth_source: str  # "db" o "file"
    client_id: str


def normalize_google_client_config(
    raw_config: dict[str, Any], redirect_uri: str | None = None
) -> dict[str, Any]:
    if "web" in raw_config and isinstance(raw_config["web"], dict):
        cfg = dict(raw_config["web"])
        client_type = "web"
    elif "installed" in raw_config and isinstance(raw_config["installed"], dict):
        cfg = dict(raw_config["installed"])
        client_type = "installed"
    elif "client_id" in raw_config and "client_secret" in raw_config:
        cfg = dict(raw_config)
        client_type = "web"
    else:
        raise GmailOAuthError(
            "invalid_credentials_structure", "Struttura delle credenziali Google non valida"
        )

    client_id = str(cfg.get("client_id") or "").strip()
    client_secret = str(cfg.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise GmailOAuthError("missing_client_keys", "Client ID o Client Secret mancanti")

    auth_uri = str(cfg.get("auth_uri") or "https://accounts.google.com/o/oauth2/auth")
    token_uri = str(cfg.get("token_uri") or "https://oauth2.googleapis.com/token")
    redirect_uris = list(cfg.get("redirect_uris") or [])
    if redirect_uri and redirect_uri not in redirect_uris:
        redirect_uris.insert(0, redirect_uri)

    normalized_entry = {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": auth_uri,
        "token_uri": token_uri,
        "redirect_uris": redirect_uris,
    }
    return {client_type: normalized_entry}


async def resolve_gmail_oauth_client(
    session: AsyncSession,
    settings: Settings,
    owner_id: uuid.UUID,
    *,
    redirect_uri_override: str | None = None,
) -> ResolvedOAuthClient:
    from gbrain.infrastructure.source_credentials_repository import SourceCredentialsRepository

    # 1. Priorità: database per tenant/owner
    record = await SourceCredentialsRepository.get_credentials(session, owner_id, "gmail")
    if record is not None and record.is_active:
        from gbrain.application.crypto import decrypt_json

        decrypted = decrypt_json(
            record.encrypted_credentials, settings.source_credentials_encryption_key
        )
        redirect_uri = (
            redirect_uri_override
            or record.configured_redirect_uri
            or settings.gmail_oauth_redirect_uri
            or f"{settings.public_url.rstrip('/')}/api/sources/gmail/oauth/callback"
        )
        normalized = normalize_google_client_config(decrypted, redirect_uri=redirect_uri)
        client_type = next(iter(normalized))
        client_id = record.client_id or str(normalized[client_type]["client_id"])
        return ResolvedOAuthClient(
            client_config=normalized,
            redirect_uri=redirect_uri,
            auth_source="db",
            client_id=client_id,
        )

    # 2. Fallback: secret file montato su filesystem
    if (
        settings.gmail_oauth_client_secret_file is not None
        and settings.gmail_oauth_client_secret_file.exists()
    ):
        raw_text = settings.gmail_oauth_client_secret_file.read_text(encoding="utf-8")
        parsed = json.loads(raw_text)
        redirect_uri = (
            redirect_uri_override
            or settings.gmail_oauth_redirect_uri
            or f"{settings.public_url.rstrip('/')}/api/gmail/oauth/callback"
        )
        normalized = normalize_google_client_config(parsed, redirect_uri=redirect_uri)
        client_type = next(iter(normalized))
        client_id = str(normalized[client_type]["client_id"])
        return ResolvedOAuthClient(
            client_config=normalized,
            redirect_uri=redirect_uri,
            auth_source="file",
            client_id=client_id,
        )

    raise GmailOAuthNotConfiguredError


async def is_gmail_configured(
    session: AsyncSession,
    settings: Settings,
    owner_id: uuid.UUID,
) -> tuple[bool, str | None, str | None]:
    from gbrain.infrastructure.source_credentials_repository import SourceCredentialsRepository

    record = await SourceCredentialsRepository.get_credentials(session, owner_id, "gmail")
    if record is not None and record.is_active:
        preview = record.client_id
        if preview and len(preview) > 16:
            preview = f"{preview[:12]}...{preview[-8:]}"
        return True, "db", preview

    if settings.gmail_oauth_configured:
        preview = None
        if (
            settings.gmail_oauth_client_secret_file is not None
            and settings.gmail_oauth_client_secret_file.exists()
        ):
            try:
                parsed = json.loads(
                    settings.gmail_oauth_client_secret_file.read_text(encoding="utf-8")
                )
                for key in ("web", "installed"):
                    if key in parsed and "client_id" in parsed[key]:
                        cid = str(parsed[key]["client_id"])
                        preview = f"{cid[:12]}...{cid[-8:]}" if len(cid) > 16 else cid
                        break
            except Exception:
                preview = None
        return True, "file", preview

    return False, None, None


def _default_flow_factory(settings: Settings, code_verifier: str) -> OAuthFlowLike:
    if settings.gmail_oauth_client_secret_file is None or settings.gmail_oauth_redirect_uri is None:
        raise GmailOAuthNotConfiguredError
    flow = Flow.from_client_secrets_file(
        str(settings.gmail_oauth_client_secret_file),
        scopes=[GMAIL_READONLY_SCOPE],
        redirect_uri=settings.gmail_oauth_redirect_uri,
        autogenerate_code_verifier=False,
        code_verifier=code_verifier,
    )
    return cast(OAuthFlowLike, flow)


def _identity_from_credentials(
    credentials: Credentials,
    *,
    account_id: uuid.UUID | str | None = None,
    rate_limiter: GmailRateLimiter | None = None,
) -> OAuthIdentity:
    # L'identità arriva da users.getProfile, non dal campo From dei messaggi.
    from gbrain.config import get_settings
    from gbrain.domain.connectors import RequestBudget
    from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway

    settings = get_settings()
    effective_limiter = rate_limiter or PostgresGmailRateLimiter(
        settings.database_url,
        settings.gmail_rate_limit_per_second,
    )
    effective_account_id: uuid.UUID | str = (
        account_id if account_id is not None else GMAIL_OAUTH_PRE_ACCOUNT_SCOPE
    )

    profile = OfficialGmailGateway.from_credentials(
        credentials,
        account_id=effective_account_id,
        rate_limiter=effective_limiter,
    ).get_profile(RequestBudget(max_requests=1, max_items=1))
    if not isinstance(profile, Mapping):
        raise GmailOAuthError("profile_invalid", "Profilo Gmail non valido")
    raw_email = str(profile.get("emailAddress") or "")
    email_address = normalize_email_address(raw_email)
    if email_address is None:
        raise GmailOAuthError("profile_email_invalid", "Google non ha restituito un account valido")
    return OAuthIdentity(
        external_account_id=email_address,
        primary_email=email_address,
        available_aliases=(email_address,),
    )


async def begin_oauth(
    session: AsyncSession,
    settings: Settings,
    owner: OwnerAccount,
    auth_session: AuthSession,
    *,
    redirect_uri_override: str | None = None,
    now: datetime | None = None,
    flow_factory: FlowFactory | None = None,
) -> OAuthStart:
    current = now or datetime.now(UTC)
    raw_state = secrets.token_urlsafe(48)
    code_verifier = secrets.token_urlsafe(64)

    if flow_factory is not None:
        flow = flow_factory(code_verifier)
    else:
        resolved = await resolve_gmail_oauth_client(
            session, settings, owner.id, redirect_uri_override=redirect_uri_override
        )
        flow = cast(
            OAuthFlowLike,
            Flow.from_client_config(
                resolved.client_config,
                scopes=[GMAIL_READONLY_SCOPE],
                redirect_uri=resolved.redirect_uri,
                autogenerate_code_verifier=False,
                code_verifier=code_verifier,
            ),
        )

    authorization_url, returned_state = flow.authorization_url(
        state=raw_state,
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
    )
    if not hmac.compare_digest(returned_state, raw_state):
        raise GmailOAuthError("state_generation_failed", "State OAuth non coerente")
    expires_at = current + timedelta(seconds=settings.gmail_oauth_state_ttl_seconds)
    session.add(
        GmailOAuthState(
            owner_id=owner.id,
            auth_session_id=auth_session.id,
            state_hash=_urlsafe_digest(raw_state, settings.auth_signing_secret),
            code_verifier_ciphertext=encrypt_secret(
                code_verifier.encode("ascii"),
                _get_token_encryption_key(settings),
                context=_STATE_ASSOCIATED_DATA,
            ),
            expires_at=expires_at,
        )
    )
    await session.commit()
    return OAuthStart(authorization_url=authorization_url, state=raw_state, expires_at=expires_at)


async def complete_oauth(
    session: AsyncSession,
    settings: Settings,
    owner: OwnerAccount,
    auth_session: AuthSession,
    *,
    state: str,
    code: str,
    now: datetime | None = None,
    flow_factory: FlowFactory | None = None,
    identity_resolver: IdentityResolver = _identity_from_credentials,
) -> tuple[ConnectorAccount, OAuthIdentity]:
    if not state or not code:
        raise GmailOAuthStateError("oauth_callback_invalid", "Callback OAuth incompleta")
    current = now or datetime.now(UTC)
    state_row = await session.scalar(
        select(GmailOAuthState)
        .where(
            GmailOAuthState.state_hash == _urlsafe_digest(state, settings.auth_signing_secret),
            GmailOAuthState.owner_id == owner.id,
            GmailOAuthState.auth_session_id == auth_session.id,
        )
        .with_for_update()
    )
    if state_row is None:
        raise GmailOAuthStateError("state_invalid", "State OAuth non valido per questa sessione")
    if state_row.consumed_at is not None:
        raise GmailOAuthStateError("state_replayed", "State OAuth già utilizzato")
    if state_row.expires_at <= current:
        raise GmailOAuthStateError("state_expired", "State OAuth scaduto")
    # Consumo prima dello scambio: anche un callback fallito non può essere riprodotto.
    state_row.consumed_at = current
    await session.commit()
    try:
        raw_verifier = decrypt_secret(
            state_row.code_verifier_ciphertext,
            _get_token_encryption_key(settings),
            context=_STATE_ASSOCIATED_DATA,
        )
    except GmailOAuthError:
        raw_verifier = decrypt_secret(
            state_row.code_verifier_ciphertext,
            settings.source_credentials_encryption_key,
            context=_STATE_ASSOCIATED_DATA,
        )
    verifier = raw_verifier.decode("ascii")

    if flow_factory is not None:
        flow = flow_factory(verifier)
    else:
        resolved = await resolve_gmail_oauth_client(session, settings, owner.id)
        flow = cast(
            OAuthFlowLike,
            Flow.from_client_config(
                resolved.client_config,
                scopes=[GMAIL_READONLY_SCOPE],
                redirect_uri=resolved.redirect_uri,
                autogenerate_code_verifier=False,
                code_verifier=verifier,
            ),
        )
    try:
        await asyncio.to_thread(flow.fetch_token, code=code)
    except Exception as exc:
        raise GmailOAuthError("token_exchange_failed", "Scambio OAuth Gmail non riuscito") from exc
    credentials = flow.credentials
    # Ricerca di eventuale account Gmail già persistito per questo owner
    existing_account = await session.scalar(
        select(ConnectorAccount).where(
            ConnectorAccount.owner_id == owner.id,
            ConnectorAccount.connector_kind == "gmail",
        )
    )
    profile_limiter: GmailRateLimiter = PostgresGmailRateLimiter(
        settings.database_url,
        settings.gmail_rate_limit_per_second,
    )
    if existing_account is not None:
        # Account esistente: preserva UUID account e limiter PostgreSQL per-account
        profile_account_id: uuid.UUID | str = existing_account.id
    else:
        # Pre-account (prima associazione): per evitare violazioni della FK su
        # gmail_rate_limits.connector_account_id e senza introdurre alcuna PII,
        # coordiniamo il pacing con lo scope globale stabile pre-account e il limiter PostgreSQL.
        profile_account_id = GMAIL_OAUTH_PRE_ACCOUNT_SCOPE

    try:
        identity = await asyncio.to_thread(
            identity_resolver,
            credentials,
            account_id=profile_account_id,
            rate_limiter=profile_limiter,
        )
    except TypeError:
        # Retrocompatibilità con resolver custom/legacy privi di keyword arguments
        identity = await asyncio.to_thread(identity_resolver, credentials)
    encrypted_token = encrypt_credentials(credentials, settings)
    account = await session.scalar(
        select(ConnectorAccount).where(
            ConnectorAccount.owner_id == owner.id,
            ConnectorAccount.connector_kind == "gmail",
            ConnectorAccount.external_account_id == identity.external_account_id,
        )
    )
    if account is None:
        account = ConnectorAccount(
            owner_id=owner.id,
            connector_kind="gmail",
            external_account_id=identity.external_account_id,
            primary_email=identity.primary_email,
        )
        session.add(account)
    account.primary_email = identity.primary_email
    account.status = ConnectorAccountStatus.ACTIVE
    account.oauth_reference = f"google:{identity.external_account_id}"
    account.oauth_token_ciphertext = encrypted_token
    account.oauth_token_format_version = _TOKEN_FORMAT_VERSION
    account.oauth_scopes = [GMAIL_READONLY_SCOPE]
    account.confirmed_aliases = []
    account.alias_set_version = (account.alias_set_version or 0) + 1
    account.authorized_at = current
    account.revoked_at = None
    await session.commit()
    await session.refresh(account)
    return account, identity


def revoke_google_token(token: str, *, timeout_seconds: float = 20.0) -> bool:
    body = urllib.parse.urlencode({"token": token}).encode("ascii")
    request = urllib.request.Request(
        _GOOGLE_REVOKE_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            status = getattr(response, "status", None)
            return isinstance(status, int) and 200 <= status < 300
    except (urllib.error.URLError, TimeoutError):
        return False


async def disconnect_account(
    session: AsyncSession,
    settings: Settings,
    account: ConnectorAccount,
    *,
    revoker: Revoker | None = None,
    now: datetime | None = None,
) -> RevocationResult:
    current = now or datetime.now(UTC)
    attempted = False
    remote_revoked = False
    if account.oauth_token_ciphertext is not None:
        try:
            credentials = decrypt_credentials(account, settings)
            token_to_revoke = credentials.refresh_token or credentials.token
            if token_to_revoke:
                attempted = True
                remote_revoked = await asyncio.to_thread(
                    revoker or revoke_google_token, token_to_revoke
                )
        except GmailOAuthError:
            attempted = False
    account.oauth_token_ciphertext = None
    account.oauth_token_format_version = None
    account.oauth_scopes = []
    account.confirmed_aliases = []
    account.alias_set_version = (account.alias_set_version or 0) + 1
    account.status = ConnectorAccountStatus.REVOKED
    account.revoked_at = current
    await session.commit()
    if remote_revoked:
        detail = "remote_and_local_revoked"
    elif attempted:
        detail = "remote_revocation_failed_local_disconnected"
    else:
        detail = "local_disconnected_remote_not_attempted"
    return RevocationResult(attempted, remote_revoked, detail)


def safe_oauth_error(exc: BaseException) -> dict[str, str]:
    if isinstance(exc, GmailOAuthError):
        return {"code": exc.code, "detail": exc.safe_message}
    return {"code": "gmail_oauth_error", "detail": "Operazione OAuth Gmail non riuscita"}
