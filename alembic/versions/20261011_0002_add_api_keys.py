"""add gmq_api_keys (spec 003: machine realm, Principle III)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11

Single table for API keys: prefix PK + Argon2id hash + revocation; the
plaintext key is never stored (specs/003-messaging-api-worker/data-model.md).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from gram_mq.settings import table_name

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

API_KEYS = table_name("api_keys")


def upgrade() -> None:
    op.create_table(
        API_KEYS,
        sa.Column("prefix", sa.String(length=12), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("prefix", name=f"pk_{API_KEYS}"),
    )


def downgrade() -> None:
    op.drop_table(API_KEYS)
