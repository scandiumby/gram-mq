"""API key issuance: `gram-mq key add` (spec 003: US1, FR-001) and
`gram-mq key prepare` (spec 005: US1, FR-001–FR-004).

The plaintext key exists exactly once — in the caller's terminal output.
`key add` persists (prefix, Argon2id hash); `key prepare` touches no
database at all and also returns the one-line activation material for
GRAMMQ_KEY_BOOTSTRAP_MATERIAL (stage 1 of the shell-less bootstrap).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from gram_mq.api.auth import (
    format_key_material,
    generate_api_key,
    hash_key,
)

KeyInserter = Callable[[str, str], Awaitable[None]]


async def run_key_add(*, insert: KeyInserter) -> tuple[str, str]:
    """Generate a key, persist prefix+hash, return (key, prefix) to print."""
    key, prefix = generate_api_key()
    await insert(prefix, hash_key(key))
    return key, prefix


def run_key_prepare() -> tuple[str, str, str]:
    """Generate key + activation material; return (key, prefix, material).

    Pure and synchronous by design (FR-002): runs on a machine with no
    database and no server settings — the hash is computed here so the
    server never needs the plaintext or Argon2 at activation time.
    """
    key, prefix = generate_api_key()
    return key, prefix, format_key_material(prefix, hash_key(key))
