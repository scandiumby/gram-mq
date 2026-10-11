"""API key generation and verification (spec 003: FR-001, FR-002, FR-014).

Key format: `gmq_` + 43 chars of urlsafe base64 (secrets.token_urlsafe(32));
the first 12 chars (including `gmq_`) are the lookup prefix. Only the
Argon2id hash of the full key is stored (Principle III). Verification runs
Argon2id off the event loop: the library has no async API, and a ~tens-of-ms
CPU burn would stall the loop (Constitution, Asynchrony — the sanctioned
`asyncio.to_thread` escape hatch with this justification).
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

KEY_PREFIX = "gmq_"
KEY_BODY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
KEY_RE = re.compile(r"^gmq_[A-Za-z0-9_-]{43}$")
PREFIX_LENGTH = 12

#: Repository lookup result: (key_hash, revoked) or None when unknown.
KeyRecord = tuple[str, bool]
KeyFetcher = Callable[[str], Awaitable[KeyRecord | None]]


class PasswordVerifier(Protocol):
    """The argon2 surface the verifier needs (injectable for cheap tests)."""

    def verify(self, hash: str, password: str) -> bool: ...  # noqa: A002


def generate_api_key() -> tuple[str, str]:
    """Return (full_key, prefix); the key exists only in memory/output."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:PREFIX_LENGTH]


def hash_key(key: str, hasher: PasswordHasher | None = None) -> str:
    """Argon2id-encoded hash of the full key (CLI side, not runtime code)."""
    return (hasher or PasswordHasher()).hash(key)


class ApiKeyVerifier:
    """Header-key verification: format → prefix lookup → revocation → hash.

    One SELECT and one Argon2id check per request, no caching: a revocation
    takes effect on the very next request (FR-014).
    """

    def __init__(
        self, fetch: KeyFetcher, verifier: PasswordVerifier | None = None
    ) -> None:
        self._fetch = fetch
        self._verifier = verifier or PasswordHasher()

    async def verify(self, key: str | None) -> bool:
        if key is None or KEY_RE.match(key) is None:
            return False
        record = await self._fetch(key[:PREFIX_LENGTH])
        if record is None:
            return False
        key_hash, revoked = record
        if revoked:
            return False
        try:
            # Argon2id is CPU-bound with no async API; a blocking call here
            # would stall the event loop (Constitution, Asynchrony).
            await asyncio.to_thread(self._verifier.verify, key_hash, key)
        except VerifyMismatchError:
            return False
        return True
