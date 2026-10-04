"""Bot registry: bot_slug only, no tokens or settings (Principle III)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from gram_mq.models.base import Base
from gram_mq.settings import table_name


class Bot(Base):
    __tablename__ = table_name("bots")

    bot_slug: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
