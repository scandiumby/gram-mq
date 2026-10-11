"""Unit contract of API key generation/verification (spec 003: FR-001, FR-002).

Cheap Argon2id parameters keep tests fast; production defaults are never
weakened (research R3).
"""

from __future__ import annotations

from argon2 import PasswordHasher

from gram_mq.api.auth import (
    KEY_RE,
    ApiKeyVerifier,
    generate_api_key,
    hash_key,
)

CHEAP_HASHER = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)


def _make_verifier(key: str, *, revoked: bool = False) -> ApiKeyVerifier:
    record: tuple[str, bool] | None = (hash_key(key, CHEAP_HASHER), revoked)

    async def fetch(prefix: str) -> tuple[str, bool] | None:
        if prefix == key[:12]:
            return record
        return None

    return ApiKeyVerifier(fetch, CHEAP_HASHER)


def test_generated_key_matches_format() -> None:
    key, prefix = generate_api_key()
    assert KEY_RE.match(key) is not None
    assert prefix == key[:12]
    assert prefix.startswith("gmq_")


def test_generated_keys_are_unique() -> None:
    keys = {generate_api_key()[0] for _ in range(100)}
    assert len(keys) == 100


def test_hash_is_argon2id_and_not_the_key() -> None:
    key, _ = generate_api_key()
    digest = hash_key(key)
    assert digest != key
    assert key not in digest
    assert digest.startswith("$argon2id$")


async def test_verify_accepts_its_own_key() -> None:
    key, _ = generate_api_key()
    verifier = _make_verifier(key)
    assert await verifier.verify(key) is True


async def test_verify_rejects_wrong_key_with_same_prefix() -> None:
    key, _ = generate_api_key()
    other, _ = generate_api_key()
    verifier = _make_verifier(key)
    assert await verifier.verify(other) is False


async def test_verify_rejects_unknown_prefix() -> None:
    key, _ = generate_api_key()
    verifier = _make_verifier(key)
    forged = "gmq_" + "z" * 43
    assert await verifier.verify(forged) is False


async def test_verify_rejects_revoked_key() -> None:
    key, _ = generate_api_key()
    verifier = _make_verifier(key, revoked=True)
    assert await verifier.verify(key) is False


async def test_verify_rejects_malformed_and_missing_keys() -> None:
    key, _ = generate_api_key()
    verifier = _make_verifier(key)
    assert await verifier.verify(None) is False
    assert await verifier.verify("") is False
    assert await verifier.verify("gmq_short") is False
    assert await verifier.verify("wrong_" + "a" * 43) is False
    assert await verifier.verify("gmq_" + "!" * 43) is False
