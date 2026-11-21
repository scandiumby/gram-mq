"""Inbound update journal: the inbound queue is the journal (spec 007).

Mirror of ``messages`` with inbound-shaped columns — ``update_id``
instead of ``chat_id``, ``delivered_at`` instead of ``sent_at``, no
Telegram message id — and its own fairness ledger
(``bot_inbound_claim_state``): one table shared by both queues would
make one bot's outbound and inbound work fight over a single
``last_claim_at``. No FK to ``bots``: the journal outlives a revoked bot
(migration 0003 decision, extended to inbound).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from gram_mq.models.base import Base
from gram_mq.settings import table_name

INBOUND_UPDATES_TABLE = table_name("inbound_updates")
INBOUND_LEDGER_TABLE = table_name("bot_inbound_claim_state")


class InboundStatus(enum.Enum):
    queued = "queued"
    delivering = "delivering"
    delivered = "delivered"
    failed = "failed"


class InboundUpdateRow(Base):
    __tablename__ = INBOUND_UPDATES_TABLE

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    bot_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[InboundStatus] = mapped_column(
        Enum(InboundStatus, name=table_name("inbound_status"), native_enum=True),
        nullable=False,
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Partial indexes, one per hot query — the claim/reclaim/depth triple of
# the outbound journal, plus the duplicate-observation index (research
# R13): (bot_slug, update_id) is deliberately NOT unique — a duplicate is
# legal at-least-once territory, and uniqueness would silently swallow a
# genuinely new update after Telegram re-picks update_id.
Index(
    f"ix_{INBOUND_UPDATES_TABLE}_claim",
    InboundUpdateRow.status,
    InboundUpdateRow.available_at,
    postgresql_where=text("status = 'queued'"),
)
Index(
    f"ix_{INBOUND_UPDATES_TABLE}_reclaim",
    InboundUpdateRow.lease_expires_at,
    postgresql_where=text("status = 'delivering'"),
)
Index(
    f"ix_{INBOUND_UPDATES_TABLE}_depth",
    InboundUpdateRow.bot_slug,
    postgresql_where=text("status IN ('queued', 'delivering')"),
)
Index(
    f"ix_{INBOUND_UPDATES_TABLE}_dupe",
    InboundUpdateRow.bot_slug,
    InboundUpdateRow.update_id,
)


class BotInboundClaimState(Base):
    """Inbound fairness ledger: which bot was served last."""

    __tablename__ = INBOUND_LEDGER_TABLE

    bot_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_claim_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
