"""Message journal rows: the queue and the journal are one table."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
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

MESSAGES_TABLE = table_name("messages")


class MessageStatus(enum.Enum):
    queued = "queued"
    leased = "leased"
    sent = "sent"
    failed = "failed"


class Message(Base):
    __tablename__ = MESSAGES_TABLE

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    bot_slug: Mapped[str] = mapped_column(
        String, ForeignKey(f"{table_name('bots')}.bot_slug"), nullable=False
    )
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name=table_name("message_status"), native_enum=True),
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
    telegram_message_id: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Partial indexes, one per hot query (RQ-6): claim candidates, expired-lease
# reclaim, and per-bot queue depth.
Index(
    f"ix_{MESSAGES_TABLE}_claim",
    Message.status,
    Message.available_at,
    postgresql_where=text("status = 'queued'"),
)
Index(
    f"ix_{MESSAGES_TABLE}_reclaim",
    Message.lease_expires_at,
    postgresql_where=text("status = 'leased'"),
)
Index(
    f"ix_{MESSAGES_TABLE}_depth",
    Message.bot_slug,
    postgresql_where=text("status IN ('queued', 'leased')"),
)
