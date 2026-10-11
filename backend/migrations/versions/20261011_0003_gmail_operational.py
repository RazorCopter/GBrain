"""Integrazione operativa Gmail: OAuth, job persistenti e ricerca PostgreSQL.

Revision ID: 20261011_0003
Revises: 20261011_0002
Create Date: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261011_0003"
down_revision: str | None = "20261011_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gmail_oauth_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("auth_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("code_verifier_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["owner_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_session_id"], ["auth_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index("ix_gmail_oauth_states_expires_at", "gmail_oauth_states", ["expires_at"])

    op.add_column(
        "import_jobs",
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "import_jobs",
        sa.Column("source_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("import_jobs", sa.Column("queue_job_id", sa.Integer(), nullable=True))
    op.add_column("import_jobs", sa.Column("mode", sa.String(length=32), nullable=True))
    op.add_column(
        "import_jobs",
        sa.Column(
            "policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("import_jobs", sa.Column("total_items", sa.Integer(), nullable=True))
    op.add_column("import_jobs", sa.Column("error_detail", sa.String(length=500), nullable=True))
    op.add_column(
        "import_jobs",
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "import_jobs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_import_jobs_connector_account",
        "import_jobs",
        "connector_accounts",
        ["connector_account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_import_jobs_source_policy",
        "import_jobs",
        "source_policies",
        ["source_policy_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_import_jobs_connector_account_id", "import_jobs", ["connector_account_id"])
    op.create_index("ix_import_jobs_source_policy_id", "import_jobs", ["source_policy_id"])
    op.create_index(
        "uq_import_jobs_queue_job_id",
        "import_jobs",
        ["queue_job_id"],
        unique=True,
        postgresql_where=sa.text("queue_job_id IS NOT NULL"),
    )
    op.create_index(
        "uq_import_jobs_one_active_per_gmail_account",
        "import_jobs",
        ["connector_account_id"],
        unique=True,
        postgresql_where=sa.text(
            "connector_account_id IS NOT NULL AND status IN ('pending', 'running')"
        ),
    )

    # Il testo resta nel database primario. L'indice expression evita una colonna
    # duplicata e usa la configurazione `simple`, adatta anche a messaggi multilingua.
    op.execute(
        "CREATE INDEX ix_source_revisions_gmail_fts "
        "ON source_revisions USING gin "
        "(to_tsvector('simple', coalesce(normalized_content::text, '')))"
    )


def downgrade() -> None:
    op.drop_index("ix_source_revisions_gmail_fts", table_name="source_revisions")
    op.drop_index("uq_import_jobs_one_active_per_gmail_account", table_name="import_jobs")
    op.drop_index("uq_import_jobs_queue_job_id", table_name="import_jobs")
    op.drop_index("ix_import_jobs_source_policy_id", table_name="import_jobs")
    op.drop_index("ix_import_jobs_connector_account_id", table_name="import_jobs")
    op.drop_constraint("fk_import_jobs_source_policy", "import_jobs", type_="foreignkey")
    op.drop_constraint("fk_import_jobs_connector_account", "import_jobs", type_="foreignkey")
    op.drop_column("import_jobs", "cancel_requested_at")
    op.drop_column("import_jobs", "retry_count")
    op.drop_column("import_jobs", "error_detail")
    op.drop_column("import_jobs", "total_items")
    op.drop_column("import_jobs", "policy_snapshot")
    op.drop_column("import_jobs", "mode")
    op.drop_column("import_jobs", "queue_job_id")
    op.drop_column("import_jobs", "source_policy_id")
    op.drop_column("import_jobs", "connector_account_id")
    op.drop_index("ix_gmail_oauth_states_expires_at", table_name="gmail_oauth_states")
    op.drop_table("gmail_oauth_states")
