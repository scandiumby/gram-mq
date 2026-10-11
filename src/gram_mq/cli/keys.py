"""API key issuance: `gram-mq key add` (spec 003: US1, FR-001).

The plaintext key exists exactly once — in the caller's terminal output.
The insert receives only (prefix, Argon2id hash).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from gram_mq.api.auth import generate_api_key, hash_key

KeyInserter = Callable[[str, str], Awaitable[None]]


async def run_key_add(*, insert: KeyInserter) -> tuple[str, str]:
    """Generate a key, persist prefix+hash, return (key, prefix) to print."""
    key, prefix = generate_api_key()
    await insert(prefix, hash_key(key))
    return key, prefix
