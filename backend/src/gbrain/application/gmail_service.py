from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from sqlalchemy import desc, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gbrain.application.gmail_oauth import decrypt_credentials, is_gmail_configured
from gbrain.application.gmail_sync import GmailSentConnector
from gbrain.config import Settings
from gbrain.domain.connectors import RequestBudget
from gbrain.domain.gmail import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SOURCE_KIND,
    GmailBackfillStateData,
    GmailSelection,
    GmailSyncConfigData,
    GmailSyncMode,
    build_gmail_link,
    compute_query_fingerprint,
    normalize_email_address,
    resolve_sync_mode,
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
    SourceSyncConfig,
)
from gbrain.infrastructure.gmail_gateway import OfficialGmailGateway
from gbrain.infrastructure.gmail_rate_limiter import (
    GmailRateLimiter,
    PostgresGmailRateLimiter,
)
from gbrain.infrastructure.gmail_repository import AsyncPostgresGmailRepository

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
class SyncConfigInput:
    history_after: datetime
    history_before: datetime | None = None
    selection_label: str = "SENT"
    query_filter: str | None = None
    item_limit: int = 10000
    request_budget: int = 20000
    batch_size: int = 50
    update_policy: str = "continuous"

    def validate(self) -> None:
        if self.history_after.tzinfo is None:
            raise GmailServiceError(
                "config_period_invalid", "history_after richiede datetime timezone-aware"
            )
        if self.history_before is not None:
            if self.history_before.tzinfo is None:
                raise GmailServiceError(
                    "config_period_invalid", "history_before richiede datetime timezone-aware"
                )
            if self.history_after >= self.history_before:
                raise GmailServiceError(
                    "config_period_invalid", "history_after deve precedere history_before"
                )
        if self.selection_label != "SENT":
            raise GmailServiceError("config_label_invalid", "Solo il label SENT è supportato")
        if self.query_filter is not None:
            normalized_filter = " ".join(self.query_filter.split())
            if not normalized_filter or len(normalized_filter) > 255:
                raise GmailServiceError(
                    "config_filter_invalid", "Filtro Gmail non valido (1-255 caratteri)"
                )
            if any(ord(char) < 32 for char in self.query_filter):
                raise GmailServiceError(
                    "config_filter_invalid", "Filtro Gmail con caratteri di controllo non ammesso"
                )
        if not 1 <= self.item_limit <= 100_000:
            raise GmailServiceError(
                "config_limit_invalid", "item_limit fuori intervallo (1-100000)"
            )
        if not 1 <= self.request_budget <= 200_000:
            raise GmailServiceError(
                "config_budget_invalid", "request_budget fuori intervallo (1-200000)"
            )
        if not 1 <= self.batch_size <= 500:
            raise GmailServiceError("config_batch_invalid", "batch_size fuori intervallo (1-500)")
        if self.update_policy not in ("continuous", "manual"):
            raise GmailServiceError("config_policy_invalid", "update_policy non supportata")


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


GatewayFactory = Callable[..., OfficialGmailGateway]


def gateway_for_account(
    account: ConnectorAccount,
    settings: Settings,
    *,
    rate_limiter: GmailRateLimiter | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> OfficialGmailGateway:
    limiter = rate_limiter or PostgresGmailRateLimiter(
        settings.database_url,
        settings.gmail_rate_limit_per_second,
    )
    return OfficialGmailGateway.from_credentials(
        decrypt_credentials(account, settings),
        account_id=account.id,
        rate_limiter=limiter,
        is_cancelled=is_cancelled,
    )


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
    configured, _, _ = await is_gmail_configured(session, settings, owner_id)
    return GmailStatus(
        configured=configured,
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

    # Sincronizza anche SourceSyncConfig per coerenza bidirezionale
    repo = AsyncPostgresGmailRepository(session)
    fp = compute_query_fingerprint(
        selection_label="SENT",
        after=value.after,
        before=value.before,
        aliases=account.confirmed_aliases,
    )
    cur_cfg = await repo.load_sync_config(account.id)
    rev = (
        (cur_cfg.revision + 1)
        if cur_cfg and cur_cfg.query_fingerprint != fp
        else (cur_cfg.revision if cur_cfg else 1)
    )
    await repo.upsert_sync_config(
        account.id,
        GmailSyncConfigData(
            revision=rev,
            history_after=value.after,
            history_before=value.before,
            selection_label="SENT",
            item_limit=value.item_limit,
            request_budget=value.request_budget,
            batch_size=value.batch_size,
            update_policy="continuous",
            query_fingerprint=fp,
        ),
    )
    await session.commit()
    return policy


async def get_or_create_sync_config(
    session: AsyncSession,
    account: ConnectorAccount,
) -> SourceSyncConfig:
    repo = AsyncPostgresGmailRepository(session)
    config = await repo.load_sync_config(account.id)
    if config is not None:
        return config
    policy = await session.scalar(
        select(SourcePolicy)
        .where(SourcePolicy.connector_account_id == account.id)
        .order_by(desc(SourcePolicy.created_at))
        .limit(1)
    )
    now = datetime.now(UTC)
    history_after = policy.period_after if policy else now - timedelta(days=90)
    history_before = policy.period_before if policy else None
    fp = compute_query_fingerprint(
        selection_label="SENT",
        after=history_after,
        before=history_before,
        aliases=account.confirmed_aliases,
    )
    data = GmailSyncConfigData(
        revision=1,
        history_after=history_after,
        history_before=history_before,
        selection_label="SENT",
        item_limit=policy.item_limit if policy else 10000,
        request_budget=policy.request_budget if policy else 20000,
        batch_size=policy.batch_size if policy else 50,
        update_policy="continuous",
        query_fingerprint=fp,
    )
    config = await repo.upsert_sync_config(account.id, data)
    await session.commit()
    await session.refresh(config)
    return config


async def update_sync_config(
    session: AsyncSession,
    account: ConnectorAccount,
    value: SyncConfigInput,
) -> tuple[SourceSyncConfig, bool]:
    value.validate()
    # Serializza salvataggi concorrenti e creazione job sul medesimo account.
    locked_account = await session.scalar(
        select(ConnectorAccount).where(ConnectorAccount.id == account.id).with_for_update()
    )
    if locked_account is None:
        raise GmailServiceError("gmail_not_found", "Account Gmail non trovato")
    active_job = await session.scalar(
        select(ImportJob)
        .where(
            ImportJob.connector_account_id == account.id,
            ImportJob.status.in_((ImportJobStatus.PENDING, ImportJobStatus.RUNNING)),
        )
        .limit(1)
    )
    if active_job is not None:
        raise GmailServiceError(
            "conflict_active_job",
            "Impossibile modificare la configurazione mentre è in corso un'operazione attiva",
        )

    repo = AsyncPostgresGmailRepository(session)
    current = await repo.load_sync_config(account.id)
    new_fp = compute_query_fingerprint(
        selection_label=value.selection_label,
        after=value.history_after,
        before=value.history_before,
        query_filter=value.query_filter,
        aliases=account.confirmed_aliases,
    )

    if current is None:
        new_rev = 1
        changed = True
    else:
        semantic_changed = current.query_fingerprint != new_fp
        changed = (
            semantic_changed
            or current.item_limit != value.item_limit
            or current.request_budget != value.request_budget
            or current.batch_size != value.batch_size
            or current.update_policy != value.update_policy
        )
        new_rev = (current.revision + 1) if semantic_changed else current.revision

    data = GmailSyncConfigData(
        revision=new_rev,
        history_after=value.history_after,
        history_before=value.history_before,
        selection_label=value.selection_label,
        query_filter=value.query_filter,
        item_limit=value.item_limit,
        request_budget=value.request_budget,
        batch_size=value.batch_size,
        update_policy=value.update_policy,
        query_fingerprint=new_fp,
    )
    saved = await repo.upsert_sync_config(account.id, data)

    if changed:
        backfill = await repo.load_backfill_state(account.id)
        if backfill is not None and backfill.status == "partial":
            bf_data = GmailBackfillStateData(
                config_revision=new_rev,
                query_fingerprint=new_fp,
                history_after=value.history_after,
                history_before=value.history_before or value.history_after,
                page_token=None,
                status="idle",
                items_imported=0,
            )
            await repo.upsert_backfill_state(account.id, bf_data)

    await session.commit()
    await session.refresh(saved)
    return saved, changed


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
    config = await get_or_create_sync_config(session, account)
    snapshot = policy_input(policy).snapshot()
    purpose = (
        "preview" if mode == "preview" else ("continuous" if mode == "incremental" else "history")
    )
    snapshot["purpose"] = purpose
    snapshot["config_revision"] = config.revision
    snapshot["query_fingerprint"] = config.query_fingerprint

    effective_mode: str = mode
    if mode != "preview":
        repository = AsyncPostgresGmailRepository(session)
        chk = await repository.load_checkpoint(account.id, policy.id)
        effective_mode = resolve_sync_mode(chk, mode).value

    job = ImportJob(
        source_kind=GMAIL_SOURCE_KIND,
        connector_account_id=account.id,
        source_policy_id=policy.id,
        mode=effective_mode,
        config_revision=config.revision,
        query_fingerprint=config.query_fingerprint,
        policy_snapshot=snapshot,
        checkpoint={
            "phase": "created",
            "purpose": purpose,
            "config_revision": config.revision,
            "query_fingerprint": config.query_fingerprint,
            "policy": snapshot,
            "persisted_messages": 0 if effective_mode == "preview" else None,
            "checkpoint_advanced": False if effective_mode == "preview" else None,
            "requested_mode": mode,
            "mode_resolved": effective_mode,
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
        queue_id = await enqueuer(
            job_id=str(job.id), account_id=str(account.id), mode=effective_mode
        )
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


async def preview_history_selection(
    account: ConnectorAccount,
    settings: Settings,
    *,
    after: datetime,
    before: datetime,
    item_limit: int = 10000,
    request_budget: int = 20000,
    batch_size: int = 50,
    gateway_factory: GatewayFactory = gateway_for_account,
) -> PreviewData:
    if after.tzinfo is None or before.tzinfo is None or after >= before:
        raise GmailServiceError("policy_period_invalid", "Periodo Gmail non valido")
    if not account.confirmed_aliases:
        raise GmailServiceError("alias_required", "Confermare almeno un alias prima della preview")
    gateway = await asyncio.to_thread(gateway_factory, account, settings)
    connector = GmailSentConnector(gateway, account.confirmed_aliases)
    budget = RequestBudget(max_requests=request_budget, max_items=item_limit)
    result = await asyncio.to_thread(
        connector.preview_selection,
        GmailSelection(
            after=after,
            before=before,
            limit=item_limit,
            page_size=min(batch_size, 500),
        ),
        budget,
    )
    return PreviewData(
        selected_items=result.selected_items,
        pages_read=result.pages_read,
        request_count=result.requests_used,
        state=result.state.value,
        truncated=result.state.value != "complete" or result.next_cursor_present,
        policy={
            "after": after.astimezone(UTC).isoformat(),
            "before": before.astimezone(UTC).isoformat(),
            "item_limit": item_limit,
            "request_budget": request_budget,
            "batch_size": batch_size,
        },
    )


async def enqueue_continuous_update_job(
    session: AsyncSession,
    account: ConnectorAccount,
    enqueuer: QueueEnqueuer,
) -> ImportJob:
    if not account.confirmed_aliases:
        raise GmailServiceError(
            "alias_required", "Confermare almeno un alias prima dell'aggiornamento"
        )
    if account.status != ConnectorAccountStatus.ACTIVE:
        raise GmailServiceError("gmail_not_active", "Account Gmail non attivo")

    locked_account = await session.scalar(
        select(ConnectorAccount).where(ConnectorAccount.id == account.id).with_for_update()
    )
    if locked_account is None:
        raise GmailServiceError("gmail_not_found", "Account Gmail non trovato")
    config = await get_or_create_sync_config(session, locked_account)
    policy = await session.scalar(
        select(SourcePolicy)
        .where(SourcePolicy.connector_account_id == account.id)
        .order_by(desc(SourcePolicy.created_at))
        .limit(1)
    )
    if policy is None:
        policy = SourcePolicy(
            connector_account_id=account.id,
            period_after=config.history_after,
            period_before=datetime.now(UTC),
            item_limit=config.item_limit,
            request_budget=config.request_budget,
            batch_size=config.batch_size,
            status=SourcePolicyStatus.ACTIVE,
        )
        session.add(policy)
        await session.commit()
        await session.refresh(policy)

    repository = AsyncPostgresGmailRepository(session)
    chk = await repository.load_checkpoint(account.id, policy.id)
    effective_mode = resolve_sync_mode(chk, None).value

    # Snapshot open-ended per l'aggiornamento continuo (nessun before statico persistito!)
    snapshot = {
        "purpose": "continuous",
        "config_revision": config.revision,
        "query_fingerprint": config.query_fingerprint,
        "after": config.history_after.astimezone(UTC).isoformat(),
        "before": None,
        "query_filter": config.query_filter,
        "item_limit": config.item_limit,
        "request_budget": config.request_budget,
        "batch_size": config.batch_size,
        "scope": GMAIL_READONLY_SCOPE,
        "perimeter": "SENT",
    }

    job = ImportJob(
        source_kind=GMAIL_SOURCE_KIND,
        connector_account_id=account.id,
        source_policy_id=policy.id,
        mode=effective_mode,
        config_revision=config.revision,
        query_fingerprint=config.query_fingerprint,
        policy_snapshot=snapshot,
        checkpoint={
            "phase": "created",
            "purpose": "continuous",
            "config_revision": config.revision,
            "query_fingerprint": config.query_fingerprint,
            "policy": snapshot,
            "persisted_messages": None,
            "checkpoint_advanced": None,
            "requested_mode": "continuous",
            "mode_resolved": effective_mode,
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
        queue_id = await enqueuer(
            job_id=str(job.id), account_id=str(account.id), mode=effective_mode
        )
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


async def enqueue_history_import_job(
    session: AsyncSession,
    account: ConnectorAccount,
    enqueuer: QueueEnqueuer,
    *,
    after: datetime | None = None,
    before: datetime | None = None,
) -> ImportJob:
    if not account.confirmed_aliases:
        raise GmailServiceError(
            "alias_required", "Confermare almeno un alias prima dell'importazione storico"
        )
    if account.status != ConnectorAccountStatus.ACTIVE:
        raise GmailServiceError("gmail_not_active", "Account Gmail non attivo")

    locked_account = await session.scalar(
        select(ConnectorAccount).where(ConnectorAccount.id == account.id).with_for_update()
    )
    if locked_account is None:
        raise GmailServiceError("gmail_not_found", "Account Gmail non trovato")
    config = await get_or_create_sync_config(session, locked_account)
    effective_after = after or config.history_after
    effective_before = before or config.history_before or datetime.now(UTC)
    if (
        effective_after.tzinfo is None
        or effective_before.tzinfo is None
        or effective_after >= effective_before
    ):
        raise GmailServiceError("policy_period_invalid", "Intervallo storico Gmail non valido")
    effective_after = effective_after.astimezone(UTC)
    effective_before = effective_before.astimezone(UTC)

    fp = compute_query_fingerprint(
        selection_label="SENT",
        after=effective_after,
        before=effective_before,
        query_filter=config.query_filter,
        aliases=account.confirmed_aliases,
    )

    policy = await session.scalar(
        select(SourcePolicy)
        .where(SourcePolicy.connector_account_id == account.id)
        .order_by(desc(SourcePolicy.created_at))
        .limit(1)
    )
    if policy is None:
        policy = SourcePolicy(
            connector_account_id=account.id,
            period_after=effective_after,
            period_before=effective_before,
            item_limit=config.item_limit,
            request_budget=config.request_budget,
            batch_size=config.batch_size,
            status=SourcePolicyStatus.ACTIVE,
        )
        session.add(policy)
        await session.commit()
        await session.refresh(policy)

    repo = AsyncPostgresGmailRepository(session)
    bf_state = await repo.load_backfill_state(account.id)
    backfill_matches_effective_selection = bool(
        bf_state
        and bf_state.config_revision == config.revision
        and bf_state.query_fingerprint == fp
        and bf_state.history_after.astimezone(UTC) == effective_after
        and bf_state.history_before.astimezone(UTC) == effective_before
    )
    resume_token = (
        bf_state.page_token
        if bf_state and backfill_matches_effective_selection and bf_state.status == "partial"
        else None
    )

    # Un backfill diverso conserva il proprio checkpoint finché il nuovo job non produce
    # un avanzamento. In questo modo un enqueue concorrente/fallito non perde il cursore storico.
    if bf_state is None or backfill_matches_effective_selection:
        await repo.upsert_backfill_state(
            account.id,
            GmailBackfillStateData(
                config_revision=config.revision,
                query_fingerprint=fp,
                history_after=effective_after,
                history_before=effective_before,
                page_token=resume_token,
                status="running",
                items_imported=bf_state.items_imported if (bf_state and resume_token) else 0,
                last_run_at=datetime.now(UTC),
            ),
        )

    snapshot = {
        "purpose": "history",
        "config_revision": config.revision,
        "query_fingerprint": fp,
        "config_query_fingerprint": config.query_fingerprint,
        "effective_query_fingerprint": fp,
        "after": effective_after.astimezone(UTC).isoformat(),
        "before": effective_before.astimezone(UTC).isoformat(),
        "query_filter": config.query_filter,
        "item_limit": config.item_limit,
        "request_budget": config.request_budget,
        "batch_size": config.batch_size,
        "scope": GMAIL_READONLY_SCOPE,
        "perimeter": "SENT",
    }

    job = ImportJob(
        source_kind=GMAIL_SOURCE_KIND,
        connector_account_id=account.id,
        source_policy_id=policy.id,
        mode=GmailSyncMode.FULL.value,
        config_revision=config.revision,
        query_fingerprint=fp,
        policy_snapshot=snapshot,
        checkpoint={
            "phase": "created",
            "purpose": "history",
            "config_revision": config.revision,
            "query_fingerprint": fp,
            "policy": snapshot,
            "resumption_token": resume_token,
            "persisted_messages": None,
            "checkpoint_advanced": None,
            "requested_mode": "history",
            "mode_resolved": GmailSyncMode.FULL.value,
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
        queue_id = await enqueuer(
            job_id=str(job.id), account_id=str(account.id), mode=GmailSyncMode.FULL.value
        )
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


async def count_imported_sources(
    session: AsyncSession,
    owner_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
) -> int:
    query = (
        select(func.count(SourceItem.id))
        .join(ConnectorAccount, ConnectorAccount.id == SourceItem.connector_account_id)
        .where(
            ConnectorAccount.owner_id == owner_id,
            SourceItem.source_kind == GMAIL_SOURCE_KIND,
            SourceItem.source_state == SourceItemState.ACTIVE,
        )
    )
    if account_id:
        query = query.where(SourceItem.connector_account_id == account_id)
    if after:
        query = query.where(SourceItem.occurred_at >= after)
    if before:
        query = query.where(SourceItem.occurred_at < before)
    val = await session.scalar(query)
    return int(val or 0)


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


async def count_search_sources(
    session: AsyncSession,
    owner_id: uuid.UUID,
    query_text: str,
    *,
    account_id: uuid.UUID | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
) -> int:
    clean = " ".join(query_text.split())
    if len(clean) < 2 or len(clean) > 200:
        raise GmailServiceError("search_query_invalid", "Ricerca da 2 a 200 caratteri richiesta")
    base_query = _owner_source_query(owner_id).where(
        text(
            "to_tsvector('simple', coalesce(source_revisions.normalized_content::text, '')) "
            "@@ websearch_to_tsquery('simple', :gmail_query)"
        )
    )
    if account_id:
        base_query = base_query.where(SourceItem.connector_account_id == account_id)
    if after:
        base_query = base_query.where(SourceItem.occurred_at >= after)
    if before:
        base_query = base_query.where(SourceItem.occurred_at < before)
    count_query = select(func.count()).select_from(base_query.subquery())
    total = await session.scalar(count_query.params(gmail_query=clean))
    return int(total or 0)


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
