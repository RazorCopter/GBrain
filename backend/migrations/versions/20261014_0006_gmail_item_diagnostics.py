"""Diagnostica per-item Gmail, tracciamento lacune e isolamento contatori.

Revision ID: 20261014_0006
Revises: 20261013_0005
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261014_0006"
down_revision: str | None = "20261013_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Valore di stato aggiuntivo per job con lacune gestite
    op.execute(
        sa.text("ALTER TYPE import_job_status ADD VALUE IF NOT EXISTS 'completed_with_warnings'")
    )

    # 2. Contatori semantici separati per import_jobs
    op.add_column(
        "import_jobs",
        sa.Column("imported_items", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "import_jobs",
        sa.Column("not_imported_items", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "import_jobs",
        sa.Column("duplicate_items", sa.Integer(), nullable=False, server_default="0"),
    )

    # Inizializza imported_items dai dati legacy esistenti in processed_items
    op.execute(
        sa.text(
            """
            UPDATE import_jobs
            SET imported_items = processed_items
            WHERE imported_items = 0 AND processed_items > 0;
            """
        )
    )

    # 3. Metadati lacune su copertura Gmail
    op.add_column(
        "gmail_coverages",
        sa.Column("has_gaps", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "gmail_coverages",
        sa.Column("missing_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "gmail_coverages",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="completed"),
    )

    # Per le coperture legacy, imposta status='legacy'
    op.execute(
        sa.text(
            """
            UPDATE gmail_coverages
            SET status = 'legacy'
            WHERE is_legacy = true;
            """
        )
    )

    # 4. Contatore non importati su backfill state
    op.add_column(
        "gmail_backfill_states",
        sa.Column("items_not_imported", sa.Integer(), nullable=False, server_default="0"),
    )

    # 5. Tabella persistente diagnostica e gap per-item
    op.create_table(
        "gmail_item_gaps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("gmail_message_id", sa.String(length=128), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=128), nullable=True),
        # 0 identifica in modo esplicito i job legacy/non revisionati. Evita la
        # semantica NULL-distinct delle UNIQUE PostgreSQL, che consentirebbe gap doppi.
        sa.Column("config_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("query_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
            ["connector_account_id"],
            ["connector_accounts.id"],
            name="fk_gmail_item_gaps_connector_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_policy_id"],
            ["source_policies.id"],
            name="fk_gmail_item_gaps_source_policy",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["import_job_id"],
            ["import_jobs.id"],
            name="fk_gmail_item_gaps_import_job",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "connector_account_id",
            "gmail_message_id",
            "config_revision",
            name="uq_gmail_item_gaps_account_msg_rev",
        ),
        sa.CheckConstraint(
            "config_revision >= 0", name="ck_gmail_item_gaps_config_revision_nonnegative"
        ),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_gmail_item_gaps_status"),
    )

    op.create_index(
        "ix_gmail_item_gaps_connector_account_id",
        "gmail_item_gaps",
        ["connector_account_id"],
    )
    op.create_index(
        "ix_gmail_item_gaps_msg_id",
        "gmail_item_gaps",
        ["gmail_message_id"],
    )
    op.create_index(
        "ix_gmail_item_gaps_account_status",
        "gmail_item_gaps",
        ["connector_account_id", "status"],
    )
    op.create_index(
        "ix_gmail_item_gaps_account_occurred",
        "gmail_item_gaps",
        ["connector_account_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_gmail_item_gaps_account_occurred", table_name="gmail_item_gaps")
    op.drop_index("ix_gmail_item_gaps_account_status", table_name="gmail_item_gaps")
    op.drop_index("ix_gmail_item_gaps_msg_id", table_name="gmail_item_gaps")
    op.drop_index("ix_gmail_item_gaps_connector_account_id", table_name="gmail_item_gaps")
    op.drop_table("gmail_item_gaps")
    op.drop_column("gmail_backfill_states", "items_not_imported")
    op.drop_column("gmail_coverages", "status")
    op.drop_column("gmail_coverages", "missing_count")
    op.drop_column("gmail_coverages", "has_gaps")
    op.drop_column("import_jobs", "duplicate_items")
    op.drop_column("import_jobs", "not_imported_items")
    op.drop_column("import_jobs", "imported_items")
