"""Rate limiting Gmail condiviso per account.

Revision ID: 20261015_0007
Revises: 20261014_0006
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261015_0007"
down_revision: str | None = "20261014_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Valore di stato terminale per job invalidati da cambio semantico/stale snapshot
    op.execute(sa.text("ALTER TYPE import_job_status ADD VALUE IF NOT EXISTS 'superseded'"))

    # 2. Tabella di rate limiting condiviso (account e pre-account globale)
    op.create_table(
        "gmail_rate_limits",
        sa.Column(
            "scope_key",
            sa.String(length=128),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "connector_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connector_accounts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "next_slot_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "blocked_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_gmail_rate_limits_connector_account_id",
        "gmail_rate_limits",
        ["connector_account_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_gmail_rate_limits_connector_account_id", table_name="gmail_rate_limits")
    op.drop_table("gmail_rate_limits")
    op.execute(sa.text("UPDATE import_jobs SET status = 'cancelled' WHERE status = 'superseded'"))
