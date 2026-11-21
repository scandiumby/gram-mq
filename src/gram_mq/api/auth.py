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
PREFIX_RE = re.compile(r"^gmq_[A-Za-z0-9_-]{8}$")
PREFIX_LENGTH = 12
#: Single source of the ApiKey column widths (models/api_keys.py sizes
#: String(...) from these); parse_key_material rejects anything longer.
KEY_HASH_MAX_LENGTH = 128

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


def format_key_material(prefix: str, key_hash: str) -> str:
    """One-line activation value: `<prefix>:<key_hash>` (spec 005, US1).

    The single copy-pasteable unit the operator transfers from the local
    terminal into GRAMMQ_KEY_BOOTSTRAP_MATERIAL; `:` appears in neither
    part, so `parse_key_material` splits unambiguously.
    """
    return f"{prefix}:{key_hash}"


def parse_key_material(value: str) -> tuple[str, str] | None:
    """Validate and split an activation value; None on any violation.

    Exactly one `:`; prefix must be the 12-char key prefix; the hash must
    be an Argon2id encoding that fits the key_hash column (spec 005,
    FR-012: reject before any write).
    """
    if value.count(":") != 1:
        return None
    prefix, key_hash = value.split(":")
    if PREFIX_RE.match(prefix) is None:
        return None
    if not key_hash.startswith("$argon2id$"):
        return None
    if len(key_hash) > KEY_HASH_MAX_LENGTH:
        return None
    return prefix, key_hash


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
