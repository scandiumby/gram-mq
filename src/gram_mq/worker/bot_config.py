"""BotConfig port and the split-storage adapter (Constitution I; spec 006).

The worker depends on the port, not on the filesystem or the DB. The
adapter reassembles the token on every claim: the registry row carries
the encrypted table half plus the pairing nonce, the bot file the
encrypted file half; both decrypt under the master key with the nonce as
AEAD associated data, then glue and a final format check. `TokenLookup`
tells the runner why a bot cannot send: a missing registry row is
permanent (revoked or unknown — US3 dead-letters immediately), anything
else (no file, NULL halves, missing master key, tamper, generation
mismatch) is retryable — the halves may reappear. The plaintext token
exists only in the adapter's return value, in memory.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gram_mq.token_store import (
    TokenFormatError,
    bot_file_path,
    decrypt_half,
    read_bot_file,
    validate_token_format,
)

#: Registry surface the adapter needs: (token_half, pair_nonce) | None.
FetchHalf = Callable[[str], Awaitable[tuple[str, str] | None]]


@dataclass(frozen=True, slots=True)
class Ready:
    token: str


@dataclass(frozen=True, slots=True)
class NotRegistered:
    """No registry row: revoked or never existed — permanent."""


@dataclass(frozen=True, slots=True)
class NotReady:
    """Registered but unreadable: retryable, the halves may reappear."""


TokenLookup = Ready | NotRegistered | NotReady


class BotConfig(Protocol):
    async def token_for(self, bot_slug: str) -> TokenLookup: ...


class SplitFileBotConfig:
    """Encrypted halves in `bots/.env-<slug>` + the bots table (FR-012)."""

    def __init__(
        self,
        *,
        bots_dir: Path,
        fetch_half: FetchHalf,
        master_key: bytes | None,
    ) -> None:
        self._bots_dir = bots_dir
        self._fetch_half = fetch_half
        self._master_key = master_key

    async def token_for(self, bot_slug: str) -> TokenLookup:
        row = await self._fetch_half(bot_slug)
        if row is None:
            return NotRegistered()
        token_half, pair_nonce = row
        key = self._master_key
        if key is None or not token_half or not pair_nonce:
            return NotReady()
        record = read_bot_file(bot_file_path(self._bots_dir, bot_slug))
        if record is None or record.pair_nonce != pair_nonce:
            return NotReady()
        aad = bytes.fromhex(pair_nonce)
        file_plain = decrypt_half(key, aad, record.file_blob)
        db_plain = decrypt_half(key, aad, token_half)
        if file_plain is None or db_plain is None:
            return NotReady()
        try:
            token_id, secret = validate_token_format(f"{file_plain}{db_plain}")
        except TokenFormatError:
            return NotReady()
        return Ready(f"{token_id}:{secret}")
