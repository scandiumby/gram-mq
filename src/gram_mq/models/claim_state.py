"""Fairness state: which bot was served last (RQ-4)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from gram_mq.models.base import Base
from gram_mq.settings import table_name


class BotClaimState(Base):
    __tablename__ = table_name("bot_claim_state")

    bot_slug: Mapped[str] = mapped_column(
        String,
        ForeignKey(f"{table_name('bots')}.bot_slug"),
        primary_key=True,
    )
    last_claim_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
