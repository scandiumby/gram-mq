"""Request schemas of /api/v1 and the bootstrap window (specs 003, 006)."""

from __future__ import annotations

from pydantic import BaseModel, Field

CHAT_ID_RE = r"^(-?\d{1,18}|@[A-Za-z0-9_]{4,64})$"
# Bot name is a public label for the bot file (spec 006, US1): non-empty,
# bounded, no control characters — the wizard's `_validate_name` rules.
BOT_NAME_RE = r"^[^\x00-\x1f\x7f]{1,64}$"


class MessageIn(BaseModel):
    bot_slug: str = Field(min_length=1, max_length=64)
    chat_id: str = Field(pattern=CHAT_ID_RE)
    text: str = Field(min_length=1, max_length=4096)


class BotCreateIn(BaseModel):
    name: str = Field(pattern=BOT_NAME_RE)
