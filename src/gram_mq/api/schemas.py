"""Request schemas of /api/v1 (spec 003: FR-004; data-model.md)."""

from __future__ import annotations

from pydantic import BaseModel, Field

CHAT_ID_RE = r"^(-?\d{1,18}|@[A-Za-z0-9_]{4,64})$"


class MessageIn(BaseModel):
    bot_slug: str = Field(min_length=1, max_length=64)
    chat_id: str = Field(pattern=CHAT_ID_RE)
    text: str = Field(min_length=1, max_length=4096)
