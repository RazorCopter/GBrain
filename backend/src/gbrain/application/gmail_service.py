from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from sqlalchemy import desc, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.application.gmail_oauth import decrypt_credentials
from gbrain.application.gmail_sync import GmailSentConnector
from gbrain.config import Settings
from gbrain.domain.connectors import RequestBudget
from gbrain.domain.gmail import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SOURCE_KIND,
    GmailSelection,
    build_gmail_link,
    normalize_email_address,
)
from gbrain.domain.models import (
    ConnectorAccount,
    ConnectorAccountStatus,
    ImportJob,
    ImportJobStatus,
    SourceItem,
    SourceItemState,
    SourcePolicy,
    SourcePolicyStatus,
    SourceRevision,
)
from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway

JobMode = Literal["preview", "full", "incremental", "reconciliation"]


class GmailServiceError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True, slots=True)
class PolicyInput:
    after: datetime
    before: datetime
    item_limit: int
    request_budget: int
    batch_size: int = 25

    def validate(self) -> None:
        if self.after.tzinfo is None or self.before.tzinfo is None or self.after >= self.before:
            raise GmailServiceError("policy_period_invalid", "Periodo Gmail non valido")
        if not 1 <= self.item_limit <= 100_000:
            raise GmailServiceError("policy_limit_invalid", "Limite Gmail fuori intervallo")
        if not 1 <= self.request_budget <= 200_000:
            raise GmailServiceError("policy_budget_invalid", "Budget Gmail fuori intervallo")
        if not 1 <= self.batch_size <= 500:
            raise GmailServiceError("policy_batch_invalid", "Dimensione batch Gmail non valida")

    def snapshot(self) -> dict[str, str | int]:
        return {
            "after": self.after.astimezone(UTC).isoformat(),
            "before": self.before.astimezone(UTC).isoformat(),
            "item_limit": self.item_limit,
            "request_budget": self.request_budget,
            "batch_size": self.batch_size,
            "scope": GMAIL_READONLY_SCOPE,
            "perimeter": "SENT",
        }


@dataclass(frozen=True, slots=True)
class GmailStatus:
    configured: bool
    connected: bool
    account: ConnectorAccount | None
    aliases_ready: bool
    capability_operations: tuple[str, ...]
    readonly_scope: str
    sent_only: bool = True


@dataclass(frozen=True, slots=True)
class PreviewData:
    selected_items: int
    pages_read: int
    request_count: int
    state: str
    truncated: bool
    policy: dict[str, str | int]


@dataclass(frozen=True, slots=True)
class CitationData:
    source_item_id: uuid.UUID
    source_revision_id: uuid.UUID
    account_id: uuid.UUID
    occurred_at: datetime | None
    subject: str | None
    snippet: str | None
    evidence: dict[str, Any]
    proof_url: str


class QueueEnqueuer(Protocol):
    async def __call__(self, *, job_id: str, account_id: str, mode: str) -> int: ...


GatewayFactory = Callable[[ConnectorAccount, Settings], OfficialGmailGateway]


def gateway_for_account(account: ConnectorAccount, settings: Settings) -> OfficialGmailGateway:
    return OfficialGmailGateway.from_credentials(decrypt_credentials(account, settings))


async def get_gmail_account(
    session: AsyncSession,
    owner_id: uuid.UUID,
    *,
    required: bool = True,
) -> ConnectorAccount | None:
    account = await session.scalar(
        select(ConnectorAccount)
        .where(
            ConnectorAccount.owner_id == owner_id,
            ConnectorAccount.connector_kind == "gmail",
        )
        .order_by(desc(ConnectorAccount.authorized_at))
        .limit(1)
    )
    if required and account is None:
        raise GmailServiceError("gmail_not_connected", "Account Gmail non collegato")
    return account


async def gmail_status(
    session: AsyncSession,
    settings: Settings,
    owner_id: uuid.UUID,
) -> GmailStatus:
    account = await get_gmail_account(session, owner_id, required=False)
    connected = bool(
        account is not None
        and account.status == ConnectorAccountStatus.ACTIVE
        and account.oauth_token_ciphertext
        and frozenset(account.oauth_scopes) == frozenset({GMAIL_READONLY_SCOPE})
    )
    operations = GmailSentConnector.inspect_capabilities  # mantiene il contratto discoverable
    del operations
    return GmailStatus(
        configured=settings.gmail_oauth_configured,
        connected=connected,
        account=account,
        aliases_ready=bool(account and account.confirmed_aliases),
        capability_operations=(
            "profile",
            "preview_sent",
            "full_import",
            "incremental_sync",
            "reconciliation",
            "fetch_sent_evidence",
            "search_imported_sources",
        ),
        readonly_scope=GMAIL_READONLY_SCOPE,
    )


def normalize_aliases(values: Sequence[str], available: Sequence[str]) -> list[str]:
    allowed = {
        normalized
        for value in available
        if (normalized := normalize_email_address(value)) is not None
    }
    normalized_values = {
        normalized for value in values if (normalized := normalize_email_address(value)) is not None
    }
    if not normalized_values:
        raise GmailServiceError("alias_required", "Confermare almeno un alias del proprietario")
    if not normalized_values <= allowed:
        raise GmailServiceError(
            "alias_not_verified", "Un alias richiesto non è stato verificato tramite Gmail"
        )
    return sorted(normalized_values)


async def confirm_aliases(
    session: AsyncSession,
    account: ConnectorAccount,
    aliases: Sequence[str],
    *,
    available_aliases: Sequence[str] | None = None,
) -> ConnectorAccount:
    available = available_aliases or (account.primary_email,)
    account.confirmed_aliases = normalize_aliases(aliases, available)
    account.alias_set_version = (account.alias_set_version or 0) + 1
    await session.commit()
    await session.refresh(account)
    return account


async def remove_alias(
    session: AsyncSession,
    account: ConnectorAccount,
    alias: str,
) -> ConnectorAccount:
    normalized = normalize_email_address(alias)
    if normalized is None:
        raise GmailServiceError("alias_invalid", "Alias Gmail non valido")
    account.confirmed_aliases = [item for item in account.confirmed_aliases if item != normalized]
    account.alias_set_version = (account.alias_set_version or 0) + 1
    await session.commit()
    await session.refresh(account)
    return account


async def save_policy(
    session: AsyncSession,
    account: ConnectorAccount,
    value: PolicyInput,
) -> SourcePolicy:
    value.validate()
    policy = await session.scalar(
        select(SourcePolicy)
        .where(SourcePolicy.connector_account_id == account.id)
        .order_by(desc(SourcePolicy.created_at))
        .limit(1)
    )
    if policy is None:
        policy = SourcePolicy(connector_account_id=account.id)
        session.add(policy)
    policy.period_after = value.after
    policy.period_before = value.before
    policy.item_limit = value.item_limit
    policy.request_budget = value.request_budget
    policy.batch_size = value.batch_size
    policy.concurrency = 1
    policy.status = SourcePolicyStatus.ACTIVE
    await session.commit()
    await session.refresh(policy)
    return policy


def policy_input(policy: SourcePolicy) -> PolicyInput:
    return PolicyInput(
        after=policy.period_after,
        before=policy.period_before,
        item_limit=policy.item_limit,
        request_budget=policy.request_budget,
        batch_size=policy.batch_size,
    )


async def preview_selection(
    account: ConnectorAccount,
    value: PolicyInput,
    settings: Settings,
    *,
    gateway_factory: GatewayFactory = gateway_for_account,
) -> PreviewData:
    value.validate()
    if not account.confirmed_aliases:
        raise GmailServiceError("alias_required", "Confermare almeno un alias prima della preview")
    gateway = await asyncio.to_thread(gateway_factory, account, settings)
    connector = GmailSentConnector(gateway, account.confirmed_aliases)
    budget = RequestBudget(max_requests=value.request_budget, max_items=value.item_limit)
    result = await asyncio.to_thread(
        connector.preview_selection,
        GmailSelection(
            after=value.after,
            before=value.before,
            limit=value.item_limit,
            page_size=min(value.batch_size, 500),
        ),
        budget,
    )
    return PreviewData(
        selected_items=result.selected_items,
        pages_read=result.pages_read,
        request_count=result.requests_used,
        state=result.state.value,
        truncated=result.state.value != "complete" or result.next_cursor_present,
        policy=value.snapshot(),
    )


async def create_and_enqueue_job(
    session: AsyncSession,
    account: ConnectorAccount,
    policy: SourcePolicy,
    mode: JobMode,
    enqueuer: QueueEnqueuer,
) -> ImportJob:
    if not account.confirmed_aliases:
        raise GmailServiceError(
            "alias_required", "Confermare almeno un alias prima dell'importazione"
        )
    if account.status != ConnectorAccountStatus.ACTIVE:
        raise GmailServiceError("gmail_not_active", "Account Gmail non attivo")
    snapshot = policy_input(policy).snapshot()
    job = ImportJob(
        source_kind=GMAIL_SOURCE_KIND,
        connector_account_id=account.id,
        source_policy_id=policy.id,
        mode=mode,
        policy_snapshot=snapshot,
        checkpoint={
            "phase": "created",
            "policy": snapshot,
            "persisted_messages": 0 if mode == "preview" else None,
            "checkpoint_advanced": False if mode == "preview" else None,
        },
        status=ImportJobStatus.PENDING,
    )
    session.add(job)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise GmailServiceError(
            "account_job_already_active", "Esiste già un lavoro Gmail attivo per questo account"
        ) from exc
    await session.refresh(job)
    try:
        queue_id = await enqueuer(job_id=str(job.id), account_id=str(account.id), mode=mode)
    except Exception as exc:
        job.status = ImportJobStatus.FAILED
        job.error_code = "enqueue_failed"
        job.error_detail = "Accodamento persistente non riuscito"
        job.finished_at = datetime.now(UTC)
        await session.commit()
        raise GmailServiceError("enqueue_failed", "Accodamento Gmail non riuscito") from exc
    job.queue_job_id = queue_id
    job.checkpoint = {**job.checkpoint, "phase": "queued", "queue_job_id": queue_id}
    await session.commit()
    await session.refresh(job)
    return job


async def request_job_cancellation(session: AsyncSession, job: ImportJob) -> ImportJob:
    if job.status not in {ImportJobStatus.PENDING, ImportJobStatus.RUNNING}:
        return job
    job.cancel_requested_at = datetime.now(UTC)
    job.checkpoint = {**job.checkpoint, "phase": "cancellation_requested"}
    await session.commit()
    await session.refresh(job)
    return job


def _owner_source_query(owner_id: uuid.UUID) -> Any:
    latest_revision = (
        select(
            SourceRevision.source_item_id.label("source_item_id"),
            func.max(SourceRevision.revision_number).label("revision_number"),
        )
        .group_by(SourceRevision.source_item_id)
        .subquery()
    )
    return (
        select(SourceItem, SourceRevision)
        .join(ConnectorAccount, ConnectorAccount.id == SourceItem.connector_account_id)
        .join(latest_revision, latest_revision.c.source_item_id == SourceItem.id)
        .join(
            SourceRevision,
            (SourceRevision.source_item_id == SourceItem.id)
            & (SourceRevision.revision_number == latest_revision.c.revision_number),
        )
        .where(
            ConnectorAccount.owner_id == owner_id,
            SourceItem.source_kind == GMAIL_SOURCE_KIND,
            SourceItem.source_state == SourceItemState.ACTIVE,
        )
    )


async def list_imported_sources(
    session: AsyncSession,
    owner_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> tuple[CitationData, ...]:
    query = _owner_source_query(owner_id)
    if account_id:
        query = query.where(SourceItem.connector_account_id == account_id)
    if after:
        query = query.where(SourceItem.occurred_at >= after)
    if before:
        query = query.where(SourceItem.occurred_at < before)
    rows = await session.execute(query.order_by(desc(SourceItem.occurred_at)).limit(limit))
    return tuple(_citation(row[0], row[1]) for row in rows)


async def search_imported_sources(
    session: AsyncSession,
    owner_id: uuid.UUID,
    query_text: str,
    *,
    account_id: uuid.UUID | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 20,
) -> tuple[CitationData, ...]:
    clean = " ".join(query_text.split())
    if len(clean) < 2 or len(clean) > 200:
        raise GmailServiceError("search_query_invalid", "Ricerca da 2 a 200 caratteri richiesta")
    query = _owner_source_query(owner_id).where(
        text(
            "to_tsvector('simple', coalesce(source_revisions.normalized_content::text, '')) "
            "@@ websearch_to_tsquery('simple', :gmail_query)"
        )
    )
    if account_id:
        query = query.where(SourceItem.connector_account_id == account_id)
    if after:
        query = query.where(SourceItem.occurred_at >= after)
    if before:
        query = query.where(SourceItem.occurred_at < before)
    rows = await session.execute(
        query.params(gmail_query=clean).order_by(desc(SourceItem.occurred_at)).limit(limit)
    )
    # L'ultima revisione per SourceItem e il limite SQL rendono i risultati deduplicati e finiti.
    return tuple(_citation(row[0], row[1]) for row in rows)


def _citation(item: SourceItem, revision: SourceRevision) -> CitationData:
    payload = revision.normalized_content
    return CitationData(
        source_item_id=item.id,
        source_revision_id=revision.id,
        account_id=cast(uuid.UUID, item.connector_account_id),
        occurred_at=item.occurred_at,
        subject=str(payload.get("subject")) if payload.get("subject") else None,
        snippet=str(payload.get("snippet")) if payload.get("snippet") else None,
        evidence=dict(revision.evidence),
        proof_url=f"/api/gmail/sources/{item.id}/proof",
    )


async def authorized_proof_url(
    session: AsyncSession,
    owner_id: uuid.UUID,
    source_item_id: uuid.UUID,
) -> str:
    item = await session.scalar(
        select(SourceItem)
        .join(ConnectorAccount, ConnectorAccount.id == SourceItem.connector_account_id)
        .where(
            SourceItem.id == source_item_id,
            ConnectorAccount.owner_id == owner_id,
            SourceItem.source_kind == GMAIL_SOURCE_KIND,
            SourceItem.source_state == SourceItemState.ACTIVE,
        )
    )
    if item is None:
        raise GmailServiceError("source_not_found", "Fonte Gmail non trovata")
    return build_gmail_link(item.external_id)


async def fetch_live_sent_evidence(
    session: AsyncSession,
    settings: Settings,
    owner_id: uuid.UUID,
    source_item_id: uuid.UUID,
    *,
    gateway_factory: GatewayFactory = gateway_for_account,
) -> dict[str, Any]:
    item = await session.scalar(
        select(SourceItem)
        .join(ConnectorAccount, ConnectorAccount.id == SourceItem.connector_account_id)
        .where(
            SourceItem.id == source_item_id,
            ConnectorAccount.owner_id == owner_id,
            SourceItem.source_kind == GMAIL_SOURCE_KIND,
        )
    )
    if item is None or item.connector_account_id is None:
        raise GmailServiceError("source_not_found", "Fonte Gmail non trovata")
    account = await session.get(ConnectorAccount, item.connector_account_id)
    if account is None or not account.confirmed_aliases:
        raise GmailServiceError("account_unavailable", "Account Gmail non disponibile")
    from gbrain.domain.gmail import GmailLocator

    gateway = await asyncio.to_thread(gateway_factory, account, settings)
    connector = GmailSentConnector(gateway, account.confirmed_aliases)
    raw = await asyncio.to_thread(
        connector.fetch_item,
        GmailLocator(message_id=item.external_id),
        RequestBudget(max_requests=1, max_items=1),
    )
    message = connector.normalize_item(raw)
    return message.provenance_payload()
