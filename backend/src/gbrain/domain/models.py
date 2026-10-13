from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ImportJobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ConnectorAccountStatus(enum.StrEnum):
    PENDING_AUTHORIZATION = "pending_authorization"
    ACTIVE = "active"
    REAUTH_REQUIRED = "reauth_required"
    REVOKED = "revoked"
    DISABLED = "disabled"


class SourcePolicyStatus(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class ReconciliationState(enum.StrEnum):
    NOT_REQUIRED = "not_required"
    REQUIRED_HISTORY_EXPIRED = "required_history_expired"
    RUNNING = "running"
    COMPLETE = "complete"


class SourceItemState(enum.StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"
    OUT_OF_SCOPE = "out_of_scope"
    EXCLUDED = "excluded"


def _enum_values(members: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in members]


class OwnerAccount(TimestampMixin, Base):
    __tablename__ = "owner_accounts"
    __table_args__ = (CheckConstraint("owner_slot = 1", name="ck_single_owner_slot"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_slot: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, default=1)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    connector_accounts: Mapped[list[ConnectorAccount]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    source_client_credentials: Mapped[list[SourceClientCredential]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("owner_accounts.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[OwnerAccount] = relationship(back_populates="sessions")


class ImportJob(TimestampMixin, Base):
    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    connector_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_accounts.id", ondelete="CASCADE"), index=True
    )
    source_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_policies.id", ondelete="RESTRICT"), index=True
    )
    queue_job_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    mode: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[ImportJobStatus] = mapped_column(
        Enum(
            ImportJobStatus,
            name="import_job_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=ImportJobStatus.PENDING,
    )
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    processed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_items: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    not_imported_items: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    duplicate_items: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    total_items: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_revision: Mapped[int | None] = mapped_column(Integer)
    query_fingerprint: Mapped[str | None] = mapped_column(String(64))


class SourceItem(TimestampMixin, Base):
    __tablename__ = "source_items"
    __table_args__ = (
        UniqueConstraint(
            "connector_account_id",
            "source_kind",
            "external_id",
            name="uq_source_item_account_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connector_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_accounts.id", ondelete="RESTRICT"), index=True
    )
    source_state: Mapped[SourceItemState] = mapped_column(
        Enum(
            SourceItemState,
            name="source_item_state",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=SourceItemState.ACTIVE,
    )
    state_reason: Mapped[str | None] = mapped_column(String(120))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    revisions: Mapped[list[SourceRevision]] = relationship(
        back_populates="source_item", cascade="all, delete-orphan"
    )


class SourceRevision(Base):
    __tablename__ = "source_revisions"
    __table_args__ = (
        UniqueConstraint("source_item_id", "revision_number", name="uq_source_revision_number"),
        UniqueConstraint("source_item_id", "content_hash", name="uq_source_revision_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_items.id", ondelete="CASCADE"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    normalized_content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    pipeline_version: Mapped[str] = mapped_column(
        String(80), nullable=False, default="foundation-v1"
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    source_history_id: Mapped[str | None] = mapped_column(String(64))
    storage_ref: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source_item: Mapped[SourceItem] = relationship(back_populates="revisions")


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("stable_key", name="uq_outbox_stable_key"),
        Index("ix_outbox_claim", "status", "available_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stable_key: Mapped[str] = mapped_column(String(200), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(
            OutboxStatus,
            name="outbox_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=OutboxStatus.PENDING,
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))


class Exclusion(Base):
    __tablename__ = "exclusions"
    __table_args__ = (
        UniqueConstraint("source_kind", "external_id_hash", name="uq_exclusion_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DeletionRecord(Base):
    __tablename__ = "deletion_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_items.id", ondelete="SET NULL")
    )
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    projection_cleanup_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class GmailOAuthState(Base):
    __tablename__ = "gmail_oauth_states"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("owner_accounts.id", ondelete="CASCADE"), nullable=False
    )
    auth_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False
    )
    state_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    code_verifier_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectorAccount(TimestampMixin, Base):
    __tablename__ = "connector_accounts"
    __table_args__ = (
        UniqueConstraint("connector_kind", "external_account_id", name="uq_connector_account"),
        CheckConstraint(
            "oauth_token_ciphertext IS NULL OR oauth_token_format_version IS NOT NULL",
            name="ck_connector_encrypted_token_versioned",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("owner_accounts.id", ondelete="CASCADE"), nullable=False
    )
    connector_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(320), nullable=False)
    primary_email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[ConnectorAccountStatus] = mapped_column(
        Enum(
            ConnectorAccountStatus,
            name="connector_account_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=ConnectorAccountStatus.PENDING_AUTHORIZATION,
    )
    oauth_reference: Mapped[str | None] = mapped_column(String(512))
    oauth_token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    oauth_token_format_version: Mapped[int | None] = mapped_column(Integer)
    oauth_scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    confirmed_aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    alias_set_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_connection_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[OwnerAccount] = relationship(back_populates="connector_accounts")
    policies: Mapped[list[SourcePolicy]] = relationship(
        back_populates="connector_account", cascade="all, delete-orphan"
    )
    checkpoint_state: Mapped[GmailCheckpoint | None] = relationship(
        back_populates="connector_account", uselist=False, cascade="all, delete-orphan"
    )
    sync_config: Mapped[SourceSyncConfig | None] = relationship(
        back_populates="connector_account", uselist=False, cascade="all, delete-orphan"
    )
    backfill_state: Mapped[GmailBackfillState | None] = relationship(
        back_populates="connector_account", uselist=False, cascade="all, delete-orphan"
    )
    coverage: Mapped[GmailCoverage | None] = relationship(
        back_populates="connector_account", uselist=False, cascade="all, delete-orphan"
    )
    gaps: Mapped[list[GmailItemGap]] = relationship(
        back_populates="connector_account", cascade="all, delete-orphan"
    )


class SourcePolicy(TimestampMixin, Base):
    __tablename__ = "source_policies"
    __table_args__ = (
        CheckConstraint("selection_label = 'SENT'", name="ck_gmail_policy_sent_only"),
        CheckConstraint("item_limit > 0", name="ck_source_policy_item_limit"),
        CheckConstraint("request_budget > 0", name="ck_source_policy_request_budget"),
        CheckConstraint("batch_size > 0 AND batch_size <= 500", name="ck_source_policy_batch_size"),
        CheckConstraint("concurrency = 1", name="ck_source_policy_concurrency_one"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connector_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    selection_label: Mapped[str] = mapped_column(String(32), nullable=False, default="SENT")
    period_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    item_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    request_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[SourcePolicyStatus] = mapped_column(
        Enum(
            SourcePolicyStatus,
            name="source_policy_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=SourcePolicyStatus.DRAFT,
    )

    connector_account: Mapped[ConnectorAccount] = relationship(back_populates="policies")


class GmailCheckpoint(TimestampMixin, Base):
    __tablename__ = "gmail_checkpoints"

    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connector_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_policies.id", ondelete="RESTRICT"), nullable=False
    )
    history_id: Mapped[str | None] = mapped_column(String(64))
    page_token: Mapped[str | None] = mapped_column(String(1024))
    coverage_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coverage_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_state: Mapped[ReconciliationState] = mapped_column(
        Enum(
            ReconciliationState,
            name="gmail_reconciliation_state",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=ReconciliationState.NOT_REQUIRED,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_revision: Mapped[int | None] = mapped_column(Integer)
    query_fingerprint: Mapped[str | None] = mapped_column(String(64))

    connector_account: Mapped[ConnectorAccount] = relationship(back_populates="checkpoint_state")


class SourceSyncConfig(TimestampMixin, Base):
    __tablename__ = "source_sync_configs"
    __table_args__ = (
        UniqueConstraint("connector_account_id", name="uq_source_sync_config_account"),
        CheckConstraint("selection_label = 'SENT'", name="ck_sync_config_sent_only"),
        CheckConstraint("item_limit > 0", name="ck_sync_config_item_limit"),
        CheckConstraint("request_budget > 0", name="ck_sync_config_request_budget"),
        CheckConstraint("batch_size > 0 AND batch_size <= 500", name="ck_sync_config_batch_size"),
        CheckConstraint("revision > 0", name="ck_sync_config_revision_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connector_accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    history_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    history_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selection_label: Mapped[str] = mapped_column(String(32), nullable=False, default="SENT")
    query_filter: Mapped[str | None] = mapped_column(String(255))
    item_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    request_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=20000)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    update_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="continuous")
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    connector_account: Mapped[ConnectorAccount] = relationship(back_populates="sync_config")


class GmailBackfillState(TimestampMixin, Base):
    __tablename__ = "gmail_backfill_states"
    __table_args__ = (
        UniqueConstraint("connector_account_id", name="uq_gmail_backfill_state_account"),
        CheckConstraint(
            "status IN ('idle', 'running', 'partial', 'completed', 'failed')",
            name="ck_backfill_state_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connector_accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    history_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    history_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    page_token: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    items_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_not_imported: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))

    connector_account: Mapped[ConnectorAccount] = relationship(back_populates="backfill_state")


class GmailCoverage(TimestampMixin, Base):
    __tablename__ = "gmail_coverages"
    __table_args__ = (UniqueConstraint("connector_account_id", name="uq_gmail_coverage_account"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connector_accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    covered_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    covered_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    query_fingerprint: Mapped[str | None] = mapped_column(String(64))
    config_revision: Mapped[int | None] = mapped_column(Integer)
    is_legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_gaps: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    missing_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="completed", server_default="completed"
    )
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    connector_account: Mapped[ConnectorAccount] = relationship(back_populates="coverage")


class GmailItemGap(TimestampMixin, Base):
    __tablename__ = "gmail_item_gaps"
    __table_args__ = (
        UniqueConstraint(
            "connector_account_id",
            "gmail_message_id",
            "config_revision",
            name="uq_gmail_item_gaps_account_msg_rev",
        ),
        CheckConstraint(
            "config_revision >= 0", name="ck_gmail_item_gaps_config_revision_nonnegative"
        ),
        CheckConstraint("status IN ('open', 'resolved')", name="ck_gmail_item_gaps_status"),
        Index("ix_gmail_item_gaps_account_status", "connector_account_id", "status"),
        Index("ix_gmail_item_gaps_account_occurred", "connector_account_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connector_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_policies.id", ondelete="SET NULL"),
    )
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_jobs.id", ondelete="SET NULL"),
    )
    gmail_message_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(128))
    config_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    query_fingerprint: Mapped[str | None] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    connector_account: Mapped[ConnectorAccount] = relationship(back_populates="gaps")


class AppConfiguration(Base):
    __tablename__ = "app_configurations"
    __table_args__ = (UniqueConstraint("key", "version", name="uq_app_configuration_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceClientCredential(TimestampMixin, Base):
    __tablename__ = "source_client_credentials"
    __table_args__ = (
        UniqueConstraint("owner_id", "source_kind", name="uq_source_client_owner_kind"),
        Index("ix_source_client_credentials_owner_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("owner_accounts.id", ondelete="CASCADE"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(32), nullable=False, default="oauth2")
    client_type: Mapped[str] = mapped_column(String(32), nullable=False, default="web")
    client_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    encrypted_credentials: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    configured_redirect_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    owner: Mapped[OwnerAccount] = relationship(back_populates="source_client_credentials")
