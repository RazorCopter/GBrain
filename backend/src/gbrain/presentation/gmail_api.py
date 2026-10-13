from __future__ import annotations

import hmac
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from gbrain.application.gmail_oauth import (
    GmailOAuthError,
    begin_oauth,
    complete_oauth,
    disconnect_account,
)
from gbrain.application.gmail_service import (
    GmailServiceError,
    PolicyInput,
    authorized_proof_url,
    confirm_aliases,
    create_and_enqueue_job,
    fetch_live_sent_evidence,
    get_gmail_account,
    gmail_status,
    list_imported_sources,
    policy_input,
    remove_alias,
    request_job_cancellation,
    save_policy,
    search_imported_sources,
)
from gbrain.domain.models import ImportJob, SourcePolicy
from gbrain.infrastructure.task_queue import GMAIL_TASK_NAME
from gbrain.presentation.dependencies import (
    AppSettings,
    CsrfPrincipalDep,
    DbSession,
    PrincipalDep,
    ProcrastinateApp,
)
from gbrain.presentation.schemas import (
    GmailAccountResponse,
    GmailAliasesRequest,
    GmailCitationResponse,
    GmailDisconnectResponse,
    GmailImportConfirmRequest,
    GmailLiveEvidenceResponse,
    GmailOAuthStartResponse,
    GmailPolicyRequest,
    GmailPolicyResponse,
    GmailPreviewResponse,
    GmailSourceListResponse,
    GmailStatusResponse,
    ImportJobResponse,
)

gmail_router = APIRouter(prefix="/api/gmail", tags=["gmail"])


def _http_error(exc: GmailOAuthError | GmailServiceError) -> HTTPException:
    code = status.HTTP_400_BAD_REQUEST
    if getattr(exc, "code", "") in {"gmail_not_connected", "source_not_found"}:
        code = status.HTTP_404_NOT_FOUND
    if getattr(exc, "code", "") in {"oauth_not_configured"}:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    if getattr(exc, "code", "") in {"account_job_already_active"}:
        code = status.HTTP_409_CONFLICT
    return HTTPException(status_code=code, detail=exc.safe_message)


def _account_response(account: object) -> GmailAccountResponse:
    from gbrain.domain.models import ConnectorAccount

    assert isinstance(account, ConnectorAccount)
    return GmailAccountResponse(
        id=account.id,
        primary_email=account.primary_email,
        status=account.status.value,
        confirmed_aliases=account.confirmed_aliases,
        aliases_ready=bool(account.confirmed_aliases),
        authorized_at=account.authorized_at,
        revoked_at=account.revoked_at,
    )


def _policy(payload: GmailPolicyRequest) -> PolicyInput:
    return PolicyInput(
        after=payload.after,
        before=payload.before,
        item_limit=payload.item_limit,
        request_budget=payload.request_budget,
        batch_size=payload.batch_size,
    )


def _citation_response(item: object) -> GmailCitationResponse:
    from gbrain.application.gmail_service import CitationData

    assert isinstance(item, CitationData)
    return GmailCitationResponse(
        source_item_id=item.source_item_id,
        source_revision_id=item.source_revision_id,
        account_id=item.account_id,
        occurred_at=item.occurred_at,
        subject=item.subject,
        snippet=item.snippet,
        evidence=item.evidence,
        proof_url=item.proof_url,
    )


@gmail_router.get("/status", response_model=GmailStatusResponse)
async def status_endpoint(
    session: DbSession,
    settings: AppSettings,
    principal: PrincipalDep,
) -> GmailStatusResponse:
    result = await gmail_status(session, settings, principal.owner.id)
    return GmailStatusResponse(
        configured=result.configured,
        connected=result.connected,
        account=_account_response(result.account) if result.account else None,
        scope=result.readonly_scope,
        scope_notice=(
            "gmail.readonly autorizza tecnicamente l'intera casella; il wrapper server-side "
            "elenca, acquisisce e riapre esclusivamente messaggi con etichetta SENT."
        ),
        sent_only=True,
        capabilities=list(result.capability_operations),
        configuration_action=(
            None
            if result.configured
            else "Montare client secret OAuth e chiave token come secret file separati."
        ),
    )


@gmail_router.post("/oauth/start", response_model=GmailOAuthStartResponse)
async def oauth_start_endpoint(
    session: DbSession,
    settings: AppSettings,
    principal: CsrfPrincipalDep,
) -> GmailOAuthStartResponse:
    try:
        result = await begin_oauth(session, settings, principal.owner, principal.session)
    except GmailOAuthError as exc:
        raise _http_error(exc) from exc
    return GmailOAuthStartResponse(
        authorization_url=result.authorization_url,
        expires_at=result.expires_at,
    )


@gmail_router.get("/oauth/callback")
async def oauth_callback_endpoint(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    principal: PrincipalDep,
    state_value: Annotated[str | None, Query(alias="state")] = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/sources?error={error}", status_code=303)
    try:
        await complete_oauth(
            session,
            settings,
            principal.owner,
            principal.session,
            state=state_value or "",
            code=code or "",
        )
    except GmailOAuthError as exc:
        request.app.state.gmail_oauth_last_error = exc.code
        return RedirectResponse(f"/sources?error={exc.safe_message}", status_code=303)
    return RedirectResponse("/sources?connected=gmail&gmail_connected=1", status_code=303)


@gmail_router.post("/disconnect", response_model=GmailDisconnectResponse)
async def disconnect_endpoint(
    session: DbSession,
    settings: AppSettings,
    principal: CsrfPrincipalDep,
) -> GmailDisconnectResponse:
    try:
        account = await get_gmail_account(session, principal.owner.id)
        assert account is not None
        result = await disconnect_account(session, settings, account)
    except (GmailOAuthError, GmailServiceError) as exc:
        raise _http_error(exc) from exc
    return GmailDisconnectResponse(
        remote_attempted=result.attempted,
        remote_revoked=result.remote_revoked,
        detail_code=result.detail_code,
    )


@gmail_router.put("/aliases", response_model=GmailAccountResponse)
async def confirm_aliases_endpoint(
    payload: GmailAliasesRequest,
    session: DbSession,
    principal: CsrfPrincipalDep,
) -> GmailAccountResponse:
    try:
        account = await get_gmail_account(session, principal.owner.id)
        assert account is not None
        account = await confirm_aliases(
            session,
            account,
            payload.aliases,
            available_aliases=(account.primary_email,),
        )
    except GmailServiceError as exc:
        raise _http_error(exc) from exc
    return _account_response(account)


@gmail_router.delete("/aliases/{alias}", response_model=GmailAccountResponse)
async def remove_alias_endpoint(
    alias: str,
    session: DbSession,
    principal: CsrfPrincipalDep,
) -> GmailAccountResponse:
    try:
        account = await get_gmail_account(session, principal.owner.id)
        assert account is not None
        account = await remove_alias(session, account, alias)
    except GmailServiceError as exc:
        raise _http_error(exc) from exc
    return _account_response(account)


@gmail_router.put("/policy", response_model=GmailPolicyResponse)
async def policy_endpoint(
    payload: GmailPolicyRequest,
    session: DbSession,
    principal: CsrfPrincipalDep,
) -> GmailPolicyResponse:
    try:
        account = await get_gmail_account(session, principal.owner.id)
        assert account is not None
        policy = await save_policy(session, account, _policy(payload))
    except GmailServiceError as exc:
        raise _http_error(exc) from exc
    return GmailPolicyResponse(
        id=policy.id,
        status=policy.status.value,
        concurrency=policy.concurrency,
        **payload.model_dump(),
    )


@gmail_router.post("/preview", response_model=GmailPreviewResponse)
async def preview_endpoint(
    payload: GmailPolicyRequest,
    session: DbSession,
    queue: ProcrastinateApp,
    principal: CsrfPrincipalDep,
) -> GmailPreviewResponse:
    try:
        account = await get_gmail_account(session, principal.owner.id)
        assert account is not None
        value = _policy(payload)
        policy = await save_policy(session, account, value)

        async def enqueue(*, job_id: str, account_id: str, mode: str) -> int:
            return await queue.configure_task(
                GMAIL_TASK_NAME,
                queue="gmail",
                lock="gmail-heavy-global",
            ).defer_async(job_id=job_id, account_id=account_id, mode=mode)

        job = await create_and_enqueue_job(session, account, policy, "preview", enqueue)
    except GmailServiceError as exc:
        raise _http_error(exc) from exc
    return GmailPreviewResponse(
        job_id=job.id,
        selected_items=0,
        pages_read=0,
        requests_used=0,
        state="queued",
        truncated=False,
        policy=value.snapshot(),
    )


@gmail_router.post("/jobs", response_model=ImportJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_job_endpoint(
    payload: GmailImportConfirmRequest,
    session: DbSession,
    queue: ProcrastinateApp,
    principal: CsrfPrincipalDep,
) -> ImportJob:
    try:
        account = await get_gmail_account(session, principal.owner.id)
        assert account is not None
        if not account.confirmed_aliases:
            raise GmailServiceError(
                "alias_required", "Confermare almeno un alias prima di avviare l'importazione"
            )
        policy = await session.scalar(
            select(SourcePolicy).where(
                SourcePolicy.id == payload.policy_id,
                SourcePolicy.connector_account_id == account.id,
            )
        )
        if policy is None:
            raise GmailServiceError("policy_not_found", "Policy Gmail non trovata")
        expected_snapshot = policy_input(policy).snapshot()
        if not hmac.compare_digest(
            str(sorted(payload.confirmed_policy.items())),
            str(sorted(expected_snapshot.items())),
        ):
            raise GmailServiceError(
                "policy_confirmation_mismatch",
                "La policy confermata non coincide con quella mostrata nella preview",
            )

        async def enqueue(*, job_id: str, account_id: str, mode: str) -> int:
            # Il payload contiene solo identificatori e modalità, mai credenziali OAuth.
            return await queue.configure_task(
                GMAIL_TASK_NAME,
                queue="gmail",
                lock="gmail-heavy-global",
            ).defer_async(job_id=job_id, account_id=account_id, mode=mode)

        return await create_and_enqueue_job(session, account, policy, payload.mode, enqueue)
    except GmailServiceError as exc:
        raise _http_error(exc) from exc


@gmail_router.get("/jobs/{job_id}", response_model=ImportJobResponse)
async def get_job_endpoint(
    job_id: uuid.UUID,
    session: DbSession,
    principal: PrincipalDep,
) -> ImportJob:
    job = await session.scalar(
        select(ImportJob)
        .join_from(ImportJob, SourcePolicy, ImportJob.source_policy_id == SourcePolicy.id)
        .where(
            ImportJob.id == job_id,
            SourcePolicy.connector_account.has(owner_id=principal.owner.id),
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Lavoro Gmail non trovato")
    return job


@gmail_router.post("/jobs/{job_id}/cancel", response_model=ImportJobResponse)
async def cancel_job_endpoint(
    job_id: uuid.UUID,
    session: DbSession,
    principal: CsrfPrincipalDep,
) -> ImportJob:
    account = await get_gmail_account(session, principal.owner.id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account Gmail non trovato")
    job = await session.scalar(
        select(ImportJob).where(
            ImportJob.id == job_id,
            ImportJob.connector_account_id == account.id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Lavoro Gmail non trovato")
    return await request_job_cancellation(session, job)


@gmail_router.get("/sources", response_model=GmailSourceListResponse)
async def sources_endpoint(
    session: DbSession,
    principal: PrincipalDep,
    account_id: uuid.UUID | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> GmailSourceListResponse:
    items = await list_imported_sources(
        session,
        principal.owner.id,
        account_id=account_id,
        after=after,
        before=before,
        limit=limit,
    )
    responses = [_citation_response(item) for item in items]
    return GmailSourceListResponse(items=responses, total=len(responses))


@gmail_router.get("/search", response_model=GmailSourceListResponse)
async def search_endpoint(
    query: Annotated[str, Query(min_length=2, max_length=200)],
    session: DbSession,
    principal: PrincipalDep,
    account_id: uuid.UUID | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> GmailSourceListResponse:
    try:
        items = await search_imported_sources(
            session,
            principal.owner.id,
            query,
            account_id=account_id,
            after=after,
            before=before,
            limit=limit,
        )
    except GmailServiceError as exc:
        raise _http_error(exc) from exc
    responses = [_citation_response(item) for item in items]
    return GmailSourceListResponse(items=responses, total=len(responses))


@gmail_router.get("/sources/{source_item_id}/proof")
async def proof_endpoint(
    source_item_id: uuid.UUID,
    session: DbSession,
    principal: PrincipalDep,
) -> RedirectResponse:
    try:
        url = await authorized_proof_url(session, principal.owner.id, source_item_id)
    except GmailServiceError as exc:
        raise _http_error(exc) from exc
    return RedirectResponse(url, status_code=303)


@gmail_router.get(
    "/sources/{source_item_id}/live-evidence", response_model=GmailLiveEvidenceResponse
)
async def live_evidence_endpoint(
    source_item_id: uuid.UUID,
    session: DbSession,
    settings: AppSettings,
    principal: PrincipalDep,
) -> GmailLiveEvidenceResponse:
    try:
        message = await fetch_live_sent_evidence(
            session, settings, principal.owner.id, source_item_id
        )
    except (GmailOAuthError, GmailServiceError) as exc:
        raise _http_error(exc) from exc
    return GmailLiveEvidenceResponse(source_item_id=source_item_id, message=message)
