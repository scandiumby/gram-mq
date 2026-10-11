"""Machine-realm API keys: prefix + Argon2id hash only (Principle III).

The plaintext key exists once — in the terminal output of `gram-mq key
add` — and nowhere else: not in this table, not in logs, not in responses.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from gram_mq.models.base import Base
from gram_mq.settings import table_name


class ApiKey(Base):
    __tablename__ = table_name("api_keys")

    prefix: Mapped[str] = mapped_column(String(12), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
