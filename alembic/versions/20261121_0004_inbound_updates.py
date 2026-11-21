"""inbound updates journal + fairness ledger + bots.webhook_secret_hmac

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-21

Spec 007 (specs/007-inbound-webhook-updates/data-model.md): the inbound
queue is its own journal (`inbound_updates`) with the same lease/attempts
mechanics as `messages` but inbound columns — update_id, delivered_at, the
queued/delivering/delivered/failed statuses. `bot_inbound_claim_state` is
a separate fairness ledger (one shared ledger would make a bot's outbound
and inbound queues fight over last_claim_at). `bots.webhook_secret_hmac`
stores the HMAC-SHA256 check value of the webhook secret (NULL = reception
off). No FKs to bots: journal and ledger outlive a revoked bot (the 0003
decision, extended to inbound); (bot_slug, update_id) is deliberately NOT
unique — absorbing is done by the broker's payload-comparing check, not a
constraint (research R13).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from gram_mq.settings import table_name

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

INBOUND = table_name("inbound_updates")
LEDGER = table_name("bot_inbound_claim_state")
BOTS = table_name("bots")
STATUS_ENUM = table_name("inbound_status")


def upgrade() -> None:
    op.create_table(
        INBOUND,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bot_slug", sa.String(length=64), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued", "delivering", "delivered", "failed", name=STATUS_ENUM
            ),
            nullable=False,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("locked_by", sa.String()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        f"ix_{INBOUND}_claim",
        INBOUND,
        ["status", "available_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        f"ix_{INBOUND}_reclaim",
        INBOUND,
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'delivering'"),
    )
    op.create_index(
        f"ix_{INBOUND}_depth",
        INBOUND,
        ["bot_slug"],
        postgresql_where=sa.text("status IN ('queued', 'delivering')"),
    )
    op.create_index(f"ix_{INBOUND}_dupe", INBOUND, ["bot_slug", "update_id"])

    op.create_table(
        LEDGER,
        sa.Column("bot_slug", sa.String(length=64), primary_key=True),
        sa.Column("last_claim_at", sa.DateTime(timezone=True)),
    )

    op.add_column(BOTS, sa.Column("webhook_secret_hmac", sa.String(length=64)))


def downgrade() -> None:
    op.drop_column(BOTS, "webhook_secret_hmac")
    op.drop_table(LEDGER)
    op.drop_index(f"ix_{INBOUND}_dupe", table_name=INBOUND)
    op.drop_index(f"ix_{INBOUND}_depth", table_name=INBOUND)
    op.drop_index(f"ix_{INBOUND}_reclaim", table_name=INBOUND)
    op.drop_index(f"ix_{INBOUND}_claim", table_name=INBOUND)
    op.drop_table(INBOUND)
    op.execute(f"DROP TYPE IF EXISTS {STATUS_ENUM}")
