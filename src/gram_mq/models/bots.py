"""Bot registry: slug + encrypted token half (Principle III v3.0.0).

The plaintext token is never stored: the table keeps only the AESGCM
table half (`secret[4:]`) and the pairing nonce shared with the file
half. NULL halves mark a pre-feature row — the bot reads as "not ready".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from gram_mq.models.base import Base
from gram_mq.settings import table_name


class Bot(Base):
    __tablename__ = table_name("bots")

    bot_slug: Mapped[str] = mapped_column(String, primary_key=True)
    token_half: Mapped[str | None] = mapped_column(Text, nullable=True)
    pair_nonce: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
