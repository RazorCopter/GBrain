from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.application.auth import authenticate_session, validate_csrf
from gbrain.config import Settings
from gbrain.domain.models import AuthSession, OwnerAccount
from gbrain.infrastructure.database import Database
from gbrain.infrastructure.graph import GraphProjection


@dataclass(slots=True)
class Principal:
    owner: OwnerAccount
    session: AuthSession


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def get_graph(request: Request) -> GraphProjection:
    return cast(GraphProjection, request.app.state.graph)


async def get_db_session(request: Request):  # type: ignore[no-untyped-def]
    database = cast(Database, request.app.state.database)
    async for session in database.session():
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


async def require_principal(
    request: Request,
    settings: AppSettings,
    session: DbSession,
) -> Principal:
    session_cookie = request.cookies.get(settings.session_cookie_name)
    authenticated = await authenticate_session(session, settings, session_cookie)
    if authenticated is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Autenticazione richiesta"
        )
    owner, auth_session = authenticated
    return Principal(owner=owner, session=auth_session)


PrincipalDep = Annotated[Principal, Depends(require_principal)]


async def require_csrf(
    request: Request,
    settings: AppSettings,
    principal: PrincipalDep,
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> Principal:
    csrf_cookie = request.cookies.get("gbrain_csrf")
    if not validate_csrf(settings, principal.session, csrf_token, csrf_cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token CSRF non valido")
    return principal


CsrfPrincipalDep = Annotated[Principal, Depends(require_csrf)]
