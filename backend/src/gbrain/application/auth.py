from __future__ import annotations

import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.config import Settings
from gbrain.domain.models import AuthSession, OwnerAccount, SystemState

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
_password_hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=2)


class AuthError(Exception):
    pass


class InvalidCredentialsError(AuthError):
    pass


class BootstrapClosedError(AuthError):
    pass


class ValidationError(AuthError):
    pass


@dataclass(frozen=True, slots=True)
class IssuedSession:
    raw_token: str
    csrf_token: str
    expires_at: datetime


def _digest(value: str, secret: str) -> bytes:
    return hmac.digest(secret.encode("utf-8"), value.encode("utf-8"), "sha256")


def _normalize_username(username: str) -> str:
    normalized = username.strip().casefold()
    if not _USERNAME_RE.fullmatch(normalized):
        raise ValidationError(
            "Il nome utente richiede 3-80 caratteri: lettere minuscole, numeri, punto, _ o -."
        )
    return normalized


def _validate_password(password: str) -> None:
    if len(password) < 14 or len(password) > 256:
        raise ValidationError("La password deve contenere da 14 a 256 caratteri.")


def _public_owner(owner: OwnerAccount) -> dict[str, str]:
    return {
        "id": str(owner.id),
        "username": owner.username,
        "display_name": owner.display_name,
    }


async def bootstrap_status(session: AsyncSession) -> bool:
    owner_count = await session.scalar(select(func.count()).select_from(OwnerAccount))
    state = await session.get(SystemState, "owner_bootstrap")
    return owner_count == 0 and state is None


async def bootstrap_owner(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str,
    display_name: str,
    password: str,
) -> tuple[dict[str, str], IssuedSession]:
    normalized = _normalize_username(username)
    clean_name = display_name.strip()
    if not clean_name or len(clean_name) > 160:
        raise ValidationError(
            "Il nome visualizzato è obbligatorio e non può superare 160 caratteri."
        )
    _validate_password(password)

    async with session.begin():
        await session.execute(select(func.pg_advisory_xact_lock(0x47425241494E)))
        if not await bootstrap_status(session):
            raise BootstrapClosedError("Il bootstrap è chiuso.")
        owner = OwnerAccount(
            owner_slot=1,
            username=normalized,
            display_name=clean_name,
            password_hash=_password_hasher.hash(password),
        )
        session.add(owner)
        await session.flush()
        session.add(
            SystemState(
                key="owner_bootstrap",
                value={"completed": True, "owner_id": str(owner.id)},
            )
        )
        issued = _new_session(owner.id, settings)
        session.add(_session_model(owner.id, issued, settings))

    return _public_owner(owner), issued


async def login(
    session: AsyncSession, settings: Settings, *, username: str, password: str
) -> tuple[dict[str, str], IssuedSession]:
    normalized = username.strip().casefold()
    owner = await session.scalar(
        select(OwnerAccount).where(
            OwnerAccount.username == normalized,
            OwnerAccount.is_active.is_(True),
        )
    )
    if owner is None:
        _password_hasher.hash(password)
        raise InvalidCredentialsError("Credenziali non valide.")
    valid: bool
    try:
        valid = _password_hasher.verify(owner.password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        valid = False
    if not valid:
        raise InvalidCredentialsError("Credenziali non valide.")

    if _password_hasher.check_needs_rehash(owner.password_hash):
        owner.password_hash = _password_hasher.hash(password)
        owner.password_changed_at = datetime.now(UTC)

    issued = _new_session(owner.id, settings)
    session.add(_session_model(owner.id, issued, settings))
    await session.commit()
    return _public_owner(owner), issued


def _new_session(owner_id: uuid.UUID, settings: Settings) -> IssuedSession:
    del owner_id
    return IssuedSession(
        raw_token=secrets.token_urlsafe(48),
        csrf_token=secrets.token_urlsafe(32),
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds),
    )


def _session_model(owner_id: uuid.UUID, issued: IssuedSession, settings: Settings) -> AuthSession:
    return AuthSession(
        owner_id=owner_id,
        token_hash=_digest(issued.raw_token, settings.auth_signing_secret),
        csrf_hash=_digest(issued.csrf_token, settings.auth_signing_secret),
        expires_at=issued.expires_at,
    )


async def authenticate_session(
    session: AsyncSession, settings: Settings, raw_token: str | None
) -> tuple[OwnerAccount, AuthSession] | None:
    if not raw_token:
        return None
    now = datetime.now(UTC)
    auth_session = await session.scalar(
        select(AuthSession).where(
            AuthSession.token_hash == _digest(raw_token, settings.auth_signing_secret),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
    )
    if auth_session is None:
        return None
    owner = await session.get(OwnerAccount, auth_session.owner_id)
    if owner is None or not owner.is_active:
        return None
    auth_session.last_seen_at = now
    await session.commit()
    return owner, auth_session


def validate_csrf(
    settings: Settings,
    auth_session: AuthSession,
    csrf_token: str | None,
    csrf_cookie: str | None,
) -> bool:
    if not csrf_token or not csrf_cookie:
        return False
    if not hmac.compare_digest(csrf_token, csrf_cookie):
        return False
    return hmac.compare_digest(
        auth_session.csrf_hash,
        _digest(csrf_token, settings.auth_signing_secret),
    )


async def logout(session: AsyncSession, auth_session: AuthSession) -> None:
    auth_session.revoked_at = datetime.now(UTC)
    await session.commit()


async def purge_expired_sessions(session: AsyncSession) -> int:
    count = int(
        await session.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.expires_at <= datetime.now(UTC))
        )
        or 0
    )
    await session.execute(delete(AuthSession).where(AuthSession.expires_at <= datetime.now(UTC)))
    await session.commit()
    return count


async def administrative_reset_bootstrap(session: AsyncSession, *, confirmation: str) -> None:
    """Procedura esplicita CLI: rimuove owner e riapre il bootstrap."""
    if confirmation != "RESET-OWNER-GBRAIN":
        raise ValidationError("Conferma amministrativa non valida.")
    async with session.begin():
        await session.execute(delete(OwnerAccount))
        await session.execute(delete(SystemState).where(SystemState.key == "owner_bootstrap"))
