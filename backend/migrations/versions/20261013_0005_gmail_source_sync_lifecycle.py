"""Gestione persistente della fonte Gmail: configurazione separata, backfill, copertura e checkpoint protetti.

Revision ID: 20261013_0005
Revises: 20261012_0004
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261013_0005"
down_revision: str | None = "20261012_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Tabella di configurazione persistente della sorgente
    op.create_table(
        "source_sync_configs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("history_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("history_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selection_label", sa.String(length=32), nullable=False, server_default="SENT"),
        sa.Column("query_filter", sa.String(length=255), nullable=True),
        sa.Column("item_limit", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column("request_budget", sa.Integer(), nullable=False, server_default="20000"),
        sa.Column("batch_size", sa.Integer(), nullable=False, server_default="50"),
        sa.Column(
            "update_policy", sa.String(length=32), nullable=False, server_default="continuous"
        ),
        sa.Column("query_fingerprint", sa.String(length=64), nullable=False),
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
        sa.UniqueConstraint("connector_account_id", name="uq_source_sync_config_account"),
        sa.CheckConstraint("selection_label = 'SENT'", name="ck_sync_config_sent_only"),
        sa.CheckConstraint("item_limit > 0", name="ck_sync_config_item_limit"),
        sa.CheckConstraint("request_budget > 0", name="ck_sync_config_request_budget"),
        sa.CheckConstraint(
            "batch_size > 0 AND batch_size <= 500", name="ck_sync_config_batch_size"
        ),
        sa.CheckConstraint("revision > 0", name="ck_sync_config_revision_positive"),
    )
    op.create_index(
        "ix_source_sync_configs_connector_account_id",
        "source_sync_configs",
        ["connector_account_id"],
    )

    # 2. Tabella stato e cursore del backfill/import storico
    op.create_table(
        "gmail_backfill_states",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("query_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("history_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("history_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("page_token", sa.String(length=1024), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="idle"),
        sa.Column("items_imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
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
        sa.UniqueConstraint("connector_account_id", name="uq_gmail_backfill_state_account"),
        sa.CheckConstraint(
            "status IN ('idle', 'running', 'partial', 'completed', 'failed')",
            name="ck_backfill_state_status",
        ),
    )
    op.create_index(
        "ix_gmail_backfill_states_connector_account_id",
        "gmail_backfill_states",
        ["connector_account_id"],
    )

    # 3. Tabella copertura effettivamente completata
    op.create_table(
        "gmail_coverages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("covered_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("covered_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("query_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("config_revision", sa.Integer(), nullable=True),
        sa.Column("is_legacy", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("connector_account_id", name="uq_gmail_coverage_account"),
    )
    op.create_index(
        "ix_gmail_coverages_connector_account_id",
        "gmail_coverages",
        ["connector_account_id"],
    )

    # 4. Colonne additive su gmail_checkpoints
    op.add_column("gmail_checkpoints", sa.Column("config_revision", sa.Integer(), nullable=True))
    op.add_column(
        "gmail_checkpoints", sa.Column("query_fingerprint", sa.String(length=64), nullable=True)
    )

    # 5. Colonne additive su import_jobs
    op.add_column("import_jobs", sa.Column("config_revision", sa.Integer(), nullable=True))
    op.add_column(
        "import_jobs", sa.Column("query_fingerprint", sa.String(length=64), nullable=True)
    )

    # 6. Bootstrap controllato per account esistenti (retrocompatibilità senza invenzione dati)
    # Copia da source_policies a source_sync_configs per account già presenti
    op.execute(
        sa.text(
            """
            INSERT INTO source_sync_configs (
                id,
                connector_account_id,
                revision,
                history_after,
                history_before,
                selection_label,
                query_filter,
                item_limit,
                request_budget,
                batch_size,
                update_policy,
                query_fingerprint,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                sp.connector_account_id,
                1,
                sp.period_after,
                sp.period_before,
                sp.selection_label,
                NULL,
                sp.item_limit,
                sp.request_budget,
                sp.batch_size,
                'continuous',
                'legacy_bootstrap',
                sp.created_at,
                sp.updated_at
            FROM source_policies sp
            INNER JOIN (
                SELECT connector_account_id, MAX(created_at) AS max_created
                FROM source_policies
                GROUP BY connector_account_id
            ) latest ON sp.connector_account_id = latest.connector_account_id AND sp.created_at = latest.max_created
            ON CONFLICT (connector_account_id) DO NOTHING;
            """
        )
    )

    # Copia copertura legacy (con is_legacy=true e nessun falso completato senza verifica)
    op.execute(
        sa.text(
            """
            INSERT INTO gmail_coverages (
                id,
                connector_account_id,
                covered_after,
                covered_before,
                query_fingerprint,
                config_revision,
                is_legacy,
                last_completed_at,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                gc.connector_account_id,
                gc.coverage_after,
                gc.coverage_before,
                'legacy_bootstrap',
                1,
                true,
                gc.last_sync_at,
                gc.created_at,
                gc.updated_at
            FROM gmail_checkpoints gc
            WHERE gc.coverage_after IS NOT NULL OR gc.coverage_before IS NOT NULL
            ON CONFLICT (connector_account_id) DO NOTHING;
            """
        )
    )

    # Inizializza stato backfill a 'idle' per account con configurazione
    op.execute(
        sa.text(
            """
            INSERT INTO gmail_backfill_states (
                id,
                connector_account_id,
                config_revision,
                query_fingerprint,
                history_after,
                history_before,
                page_token,
                status,
                items_imported,
                last_run_at,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                sc.connector_account_id,
                sc.revision,
                sc.query_fingerprint,
                sc.history_after,
                coalesce(sc.history_before, sc.history_after),
                NULL,
                'idle',
                0,
                NULL,
                sc.created_at,
                sc.updated_at
            FROM source_sync_configs sc
            ON CONFLICT (connector_account_id) DO NOTHING;
            """
        )
    )


def downgrade() -> None:
    op.drop_column("import_jobs", "query_fingerprint")
    op.drop_column("import_jobs", "config_revision")
    op.drop_column("gmail_checkpoints", "query_fingerprint")
    op.drop_column("gmail_checkpoints", "config_revision")
    op.drop_table("gmail_coverages")
    op.drop_table("gmail_backfill_states")
    op.drop_table("source_sync_configs")
