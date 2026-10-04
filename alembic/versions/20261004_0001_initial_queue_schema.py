"""initial queue schema: bots, messages, bot_claim_state

Revision ID: 0001
Revises:
Create Date: 2026-10-04

Tables and partial indexes per specs/001-broker-port-postgres/data-model.md
and RQ-6: claim candidates, expired-lease reclaim, per-bot depth.

All DB object names are prefixed via GRAMMQ_TABLE_PREFIX (RQ-11, default
`gmq`). The prefix is fixed when this first migration is applied: renaming
it on an existing database is not supported.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from gram_mq.settings import table_name

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

BOTS = table_name("bots")
CLAIM_STATE = table_name("bot_claim_state")
MESSAGES = table_name("messages")
STATUS_ENUM = table_name("message_status")


def upgrade() -> None:
    op.create_table(
        BOTS,
        sa.Column("bot_slug", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("bot_slug", name=f"pk_{BOTS}"),
    )

    op.create_table(
        CLAIM_STATE,
        sa.Column("bot_slug", sa.String(), nullable=False),
        sa.Column("last_claim_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["bot_slug"],
            [f"{BOTS}.bot_slug"],
            name=f"fk_{CLAIM_STATE}_bot_slug_{BOTS}",
        ),
        sa.PrimaryKeyConstraint("bot_slug", name=f"pk_{CLAIM_STATE}"),
    )

    # No explicit .create(): the same instance is passed to the messages
    # table below, and SQLAlchemy's before-create hook emits CREATE TYPE
    # exactly once; an extra explicit call would duplicate it.
    message_status = postgresql.ENUM(
        "queued", "leased", "sent", "failed", name=STATUS_ENUM
    )

    op.create_table(
        MESSAGES,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bot_slug", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", message_status, nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("telegram_message_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["bot_slug"], [f"{BOTS}.bot_slug"], name=f"fk_{MESSAGES}_bot_slug_{BOTS}"
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{MESSAGES}"),
    )

    op.create_index(
        f"ix_{MESSAGES}_claim",
        MESSAGES,
        ["status", "available_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        f"ix_{MESSAGES}_reclaim",
        MESSAGES,
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'leased'"),
    )
    op.create_index(
        f"ix_{MESSAGES}_depth",
        MESSAGES,
        ["bot_slug"],
        postgresql_where=sa.text("status IN ('queued', 'leased')"),
    )


def downgrade() -> None:
    op.drop_index(f"ix_{MESSAGES}_depth", table_name=MESSAGES)
    op.drop_index(f"ix_{MESSAGES}_reclaim", table_name=MESSAGES)
    op.drop_index(f"ix_{MESSAGES}_claim", table_name=MESSAGES)
    op.drop_table(MESSAGES)
    postgresql.ENUM(name=STATUS_ENUM).drop(op.get_bind())
    op.drop_table(CLAIM_STATE)
    op.drop_table(BOTS)
