"""BotConfig port and the file adapter (Constitution I; spec 003 FR-008).

The worker depends on the port, not on the filesystem. The adapter reuses
the parser of feature 002, so the token file format has exactly one
contract. The token is re-read on every claim: re-issuing a bot token and
replacing the file takes effect on the next attempt without a worker
restart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from gram_mq.cli.token import read_token_file


class BotConfig(Protocol):
    async def token_for(self, bot_slug: str) -> str | None:
        """`{id}:{secret}` or None when no readable valid config file."""
        ...


class FileBotConfig:
    """Reads `bots/.env-<bot_slug>`; missing or damaged file → None."""

    def __init__(self, bots_dir: Path) -> None:
        self._bots_dir = bots_dir

    async def token_for(self, bot_slug: str) -> str | None:
        parsed = read_token_file(self._bots_dir / f".env-{bot_slug}")
        if parsed is None:
            return None
        token_id, secret = parsed
        return f"{token_id}:{secret}"
