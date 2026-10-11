"""Nucleo deterministico del connettore Gmail SENT.

Revision ID: 20261011_0002
Revises: 20261010_0001
Create Date: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261011_0002"
down_revision: str | None = "20261010_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

connector_account_status = postgresql.ENUM(
    "pending_authorization",
    "active",
    "reauth_required",
    "revoked",
    "disabled",
    name="connector_account_status",
)
source_policy_status = postgresql.ENUM(
    "draft", "active", "paused", "disabled", name="source_policy_status"
)
gmail_reconciliation_state = postgresql.ENUM(
    "not_required",
    "required_history_expired",
    "running",
    "complete",
    name="gmail_reconciliation_state",
)
source_item_state = postgresql.ENUM(
    "active", "removed", "out_of_scope", "excluded", name="source_item_state"
)


def upgrade() -> None:
    bind = op.get_bind()
    connector_account_status.create(bind, checkfirst=True)
    source_policy_status.create(bind, checkfirst=True)
    gmail_reconciliation_state.create(bind, checkfirst=True)
    source_item_state.create(bind, checkfirst=True)

    op.create_table(
        "connector_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_kind", sa.String(length=64), nullable=False),
        sa.Column("external_account_id", sa.String(length=320), nullable=False),
        sa.Column("primary_email", sa.String(length=320), nullable=False),
        sa.Column(
            "status",
            connector_account_status,
            server_default="pending_authorization",
            nullable=False,
        ),
        sa.Column("oauth_reference", sa.String(length=512), nullable=True),
        sa.Column("oauth_token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("oauth_token_format_version", sa.Integer(), nullable=True),
        sa.Column(
            "oauth_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "confirmed_aliases",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("alias_set_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_connection_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "oauth_token_ciphertext IS NULL OR oauth_token_format_version IS NOT NULL",
            name="ck_connector_encrypted_token_versioned",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["owner_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connector_kind", "external_account_id", name="uq_connector_account"),
    )

    op.create_table(
        "source_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selection_label", sa.String(length=32), server_default="SENT", nullable=False),
        sa.Column("period_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("item_limit", sa.Integer(), nullable=False),
        sa.Column("request_budget", sa.Integer(), nullable=False),
        sa.Column("batch_size", sa.Integer(), server_default="25", nullable=False),
        sa.Column("concurrency", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", source_policy_status, server_default="draft", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("selection_label = 'SENT'", name="ck_gmail_policy_sent_only"),
        sa.CheckConstraint("item_limit > 0", name="ck_source_policy_item_limit"),
        sa.CheckConstraint("request_budget > 0", name="ck_source_policy_request_budget"),
        sa.CheckConstraint(
            "batch_size > 0 AND batch_size <= 500", name="ck_source_policy_batch_size"
        ),
        sa.CheckConstraint("concurrency = 1", name="ck_source_policy_concurrency_one"),
        sa.ForeignKeyConstraint(
            ["connector_account_id"], ["connector_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_policies_connector_account_id",
        "source_policies",
        ["connector_account_id"],
    )

    op.create_table(
        "gmail_checkpoints",
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_policy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("history_id", sa.String(length=64), nullable=True),
        sa.Column("page_token", sa.String(length=1024), nullable=True),
        sa.Column("coverage_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reconciliation_state",
            gmail_reconciliation_state,
            server_default="not_required",
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connector_account_id"], ["connector_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["source_policy_id"], ["source_policies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("connector_account_id"),
    )

    op.add_column(
        "source_items",
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "source_items",
        sa.Column("source_state", source_item_state, server_default="active", nullable=False),
    )
    op.add_column("source_items", sa.Column("state_reason", sa.String(length=120), nullable=True))
    op.add_column(
        "source_items", sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_source_items_connector_account_id",
        "source_items",
        "connector_accounts",
        ["connector_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_source_items_connector_account_id", "source_items", ["connector_account_id"]
    )
    op.drop_constraint("uq_source_item_identity", "source_items", type_="unique")
    op.create_unique_constraint(
        "uq_source_item_account_identity",
        "source_items",
        ["connector_account_id", "source_kind", "external_id"],
    )

    op.add_column(
        "source_revisions",
        sa.Column(
            "normalized_content",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "source_revisions",
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "source_revisions",
        sa.Column(
            "pipeline_version",
            sa.String(length=80),
            server_default="foundation-v1",
            nullable=False,
        ),
    )
    op.add_column(
        "source_revisions", sa.Column("idempotency_key", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "source_revisions", sa.Column("source_history_id", sa.String(length=64), nullable=True)
    )
    op.create_unique_constraint(
        "uq_source_revisions_idempotency_key", "source_revisions", ["idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_source_revisions_idempotency_key", "source_revisions", type_="unique")
    op.drop_column("source_revisions", "source_history_id")
    op.drop_column("source_revisions", "idempotency_key")
    op.drop_column("source_revisions", "pipeline_version")
    op.drop_column("source_revisions", "evidence")
    op.drop_column("source_revisions", "normalized_content")

    op.drop_constraint("uq_source_item_account_identity", "source_items", type_="unique")
    op.create_unique_constraint(
        "uq_source_item_identity", "source_items", ["source_kind", "external_id"]
    )
    op.drop_index("ix_source_items_connector_account_id", table_name="source_items")
    op.drop_constraint("fk_source_items_connector_account_id", "source_items", type_="foreignkey")
    op.drop_column("source_items", "last_observed_at")
    op.drop_column("source_items", "state_reason")
    op.drop_column("source_items", "source_state")
    op.drop_column("source_items", "connector_account_id")

    op.drop_table("gmail_checkpoints")
    op.drop_index("ix_source_policies_connector_account_id", table_name="source_policies")
    op.drop_table("source_policies")
    op.drop_table("connector_accounts")

    bind = op.get_bind()
    source_item_state.drop(bind, checkfirst=True)
    gmail_reconciliation_state.drop(bind, checkfirst=True)
    source_policy_status.drop(bind, checkfirst=True)
    connector_account_status.drop(bind, checkfirst=True)
