"""Persistenza sicura per credenziali client delle sorgenti con cifratura a riposo.

Revision ID: 20261012_0004
Revises: 20261011_0003
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261012_0004"
down_revision: str | None = "20261011_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_client_credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "auth_method",
            sa.String(length=32),
            server_default="oauth2",
            nullable=False,
        ),
        sa.Column(
            "client_type",
            sa.String(length=32),
            server_default="web",
            nullable=False,
        ),
        sa.Column("client_id", sa.String(length=512), nullable=True),
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=False),
        sa.Column(
            "format_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("configured_redirect_uri", sa.String(length=512), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
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
        sa.ForeignKeyConstraint(["owner_id"], ["owner_accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("owner_id", "source_kind", name="uq_source_client_owner_kind"),
    )
    op.create_index(
        "ix_source_client_credentials_owner_id",
        "source_client_credentials",
        ["owner_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_client_credentials_owner_id",
        table_name="source_client_credentials",
    )
    op.drop_table("source_client_credentials")
