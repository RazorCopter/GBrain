from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select

from gbrain.application.auth import (
    BootstrapClosedError,
    InvalidCredentialsError,
    ValidationError,
    bootstrap_owner,
    bootstrap_status,
    login,
    logout,
)
from gbrain.application.demo import get_demo_fixture_status
from gbrain.domain.models import ImportJob
from gbrain.presentation.dependencies import (
    AppSettings,
    CsrfPrincipalDep,
    DbSession,
    PrincipalDep,
)
from gbrain.presentation.schemas import (
    BootstrapRequest,
    DemoFixtureStatusResponse,
    ImportJobListResponse,
    ImportJobResponse,
    LoginRequest,
    OwnerResponse,
    PublicConfigResponse,
    ServiceStatus,
    SessionResponse,
    StatusResponse,
)

logger = logging.getLogger(__name__)
api_router = APIRouter(prefix="/api")


def _set_session_cookies(
    response: Response,
    settings: AppSettings,
    token: str,
    csrf_token: str,
) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_secure_cookie,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        key="gbrain_csrf",
        value=csrf_token,
        max_age=settings.session_ttl_seconds,
        httponly=False,
        secure=settings.session_secure_cookie,
        samesite="strict",
        path="/",
    )


def _clear_session_cookies(response: Response, settings: AppSettings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.session_secure_cookie,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        "gbrain_csrf",
        path="/",
        secure=settings.session_secure_cookie,
        httponly=False,
        samesite="strict",
    )


@api_router.get("/public/config", response_model=PublicConfigResponse)
async def public_config(settings: AppSettings) -> PublicConfigResponse:
    return PublicConfigResponse(
        demo_mode=settings.demo_mode,
        environment=settings.environment,
        session_ttl_seconds=settings.session_ttl_seconds,
        implemented_sources=["gmail_sent"],
    )


@api_router.get("/auth/session", response_model=SessionResponse)
async def get_session_state(
    session: DbSession, settings: AppSettings, request: Request
) -> SessionResponse:
    from gbrain.application.auth import authenticate_session

    authenticated = await authenticate_session(
        session, settings, request.cookies.get(settings.session_cookie_name)
    )
    if authenticated is None:
        return SessionResponse(
            authenticated=False,
            bootstrap_required=await bootstrap_status(session),
        )
    owner, _ = authenticated
    return SessionResponse(
        authenticated=True,
        bootstrap_required=False,
        owner=OwnerResponse(
            id=owner.id,
            username=owner.username,
            display_name=owner.display_name,
        ),
    )


@api_router.post(
    "/auth/bootstrap", response_model=SessionResponse, status_code=status.HTTP_201_CREATED
)
async def create_owner(
    payload: BootstrapRequest,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> SessionResponse:
    try:
        owner_data, issued = await bootstrap_owner(
            session,
            settings,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
        )
    except BootstrapClosedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    _set_session_cookies(response, settings, issued.raw_token, issued.csrf_token)
    return SessionResponse(
        authenticated=True,
        bootstrap_required=False,
        owner=OwnerResponse.model_validate(owner_data),
        csrf_token=issued.csrf_token,
    )


@api_router.post("/auth/login", response_model=SessionResponse)
async def create_session(
    payload: LoginRequest,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> SessionResponse:
    try:
        owner_data, issued = await login(
            session, settings, username=payload.username, password=payload.password
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    _set_session_cookies(response, settings, issued.raw_token, issued.csrf_token)
    return SessionResponse(
        authenticated=True,
        bootstrap_required=False,
        owner=OwnerResponse.model_validate(owner_data),
        csrf_token=issued.csrf_token,
    )


@api_router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    response: Response,
    session: DbSession,
    settings: AppSettings,
    principal: CsrfPrincipalDep,
) -> None:
    await logout(session, principal.session)
    _clear_session_cookies(response, settings)


@api_router.get("/profile", response_model=OwnerResponse)
async def profile(principal: PrincipalDep) -> OwnerResponse:
    return OwnerResponse(
        id=principal.owner.id,
        username=principal.owner.username,
        display_name=principal.owner.display_name,
    )


@api_router.get("/status", response_model=StatusResponse)
async def service_status(request: Request, principal: PrincipalDep) -> StatusResponse:
    del principal
    postgres = ServiceStatus(status="ok", detail="Database autorevole raggiungibile")
    try:
        await request.app.state.graph.ping()
        neo4j = ServiceStatus(status="ok", detail="Proiezione Neo4j raggiungibile")
    except Exception:
        logger.warning("Probe Neo4j non riuscito")
        neo4j = ServiceStatus(status="unavailable", detail="Proiezione Neo4j non raggiungibile")
    return StatusResponse(
        overall="ok" if neo4j.status == "ok" else "degraded",
        postgres=postgres,
        neo4j_projection=neo4j,
        graphiti=ServiceStatus(status="not_implemented", detail="Graphiti non è implementato"),
        demo_mode=request.app.state.settings.demo_mode,
    )


@api_router.get("/demo/status", response_model=DemoFixtureStatusResponse)
async def demo_fixture_status(
    session: DbSession,
    settings: AppSettings,
    principal: PrincipalDep,
) -> DemoFixtureStatusResponse:
    del principal
    fixture_status = await get_demo_fixture_status(session, enabled=settings.demo_mode)
    return DemoFixtureStatusResponse(
        enabled=fixture_status.enabled,
        seeded=fixture_status.seeded,
        dataset=fixture_status.dataset,
        marker=fixture_status.marker,
        item_count=fixture_status.item_count,
        revision_count=fixture_status.revision_count,
        import_job_count=fixture_status.import_job_count,
        expected_item_count=fixture_status.expected_item_count,
        expected_revision_count=fixture_status.expected_revision_count,
        cases=list(fixture_status.cases),
    )


@api_router.get("/imports", response_model=ImportJobListResponse)
async def list_import_jobs(
    session: DbSession,
    principal: PrincipalDep,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ImportJobListResponse:
    del principal
    jobs = list(
        (
            await session.scalars(
                select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit).offset(offset)
            )
        ).all()
    )
    total = int(await session.scalar(select(func.count()).select_from(ImportJob)) or 0)
    return ImportJobListResponse(
        items=[ImportJobResponse.model_validate(job) for job in jobs],
        total=total,
    )
