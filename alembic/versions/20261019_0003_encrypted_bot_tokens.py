"""encrypted split bot tokens: bots.token_half/pair_nonce, messages FK drop

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19

Spec 006 (specs/006-encrypted-token-storage/data-model.md): the bots table
gains the encrypted table half of the bot token plus the pairing nonce
(both NULL for pre-feature rows — such bots read as "not ready"); the
messages->bots FK is dropped so the journal outlives a revoked bot (the
registry existence check moves into PostgresBroker.enqueue explicitly).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from gram_mq.settings import table_name

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

BOTS = table_name("bots")
CLAIM_STATE = table_name("bot_claim_state")
MESSAGES = table_name("messages")
MESSAGES_BOT_FK = f"fk_{MESSAGES}_bot_slug_{BOTS}"
CLAIM_STATE_BOT_FK = f"fk_{CLAIM_STATE}_bot_slug_{BOTS}"


def upgrade() -> None:
    op.add_column(BOTS, sa.Column("token_half", sa.Text(), nullable=True))
    op.add_column(BOTS, sa.Column("pair_nonce", sa.String(length=16), nullable=True))
    # Both child FKs must go: a revoked bot's queued messages are
    # dead-lettered by the worker through the claim round-robin, which
    # iterates bot_claim_state — the ledger row (like the journal) is a
    # historical record that outlives the bot.
    op.drop_constraint(MESSAGES_BOT_FK, MESSAGES, type_="foreignkey")
    op.drop_constraint(CLAIM_STATE_BOT_FK, CLAIM_STATE, type_="foreignkey")


def downgrade() -> None:
    # Re-adding the FKs requires every child bot_slug to exist in bots:
    # a database that revoked bots after 0003 cannot downgrade past this
    # revision without reconciling the journal and the ledger first.
    op.create_foreign_key(
        MESSAGES_BOT_FK,
        MESSAGES,
        BOTS,
        ["bot_slug"],
        ["bot_slug"],
    )
    op.create_foreign_key(
        CLAIM_STATE_BOT_FK,
        CLAIM_STATE,
        BOTS,
        ["bot_slug"],
        ["bot_slug"],
    )
    op.drop_column(BOTS, "pair_nonce")
    op.drop_column(BOTS, "token_half")
