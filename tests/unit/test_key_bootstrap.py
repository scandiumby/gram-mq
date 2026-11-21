"""Unit contract of the two-stage API key bootstrap (spec 005).

Stage-1 material primitives (`format_key_material` / `parse_key_material`)
and the activation endpoint (`POST /bootstrap/api-key`) are tested here
against contracts/key-bootstrap-endpoint.md: the plaintext key and the
hash never travel over HTTP, and the material comes from settings only.
"""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gram_mq.adapters.memory.broker import InMemoryBroker, MemoryStore
from gram_mq.api.app import KeyBootstrap, create_app
from gram_mq.api.auth import (
    ApiKeyVerifier,
    format_key_material,
    parse_key_material,
)

CHEAP_HASHER = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)

PREFIX = "gmq_ab12cd34"
HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ" + "h" * 43
MATERIAL = format_key_material(PREFIX, HASH)


class FakeActivator:
    """Records insertions; `exists` prefixes report as conflicts."""

    def __init__(self, exists: set[str] | None = None) -> None:
        self.rows: dict[str, str] = {}
        self.calls = 0
        self._exists = set(exists or ())

    async def activate(self, prefix: str, key_hash: str) -> bool:
        self.calls += 1
        if prefix in self.rows or prefix in self._exists:
            return False
        self.rows[prefix] = key_hash
        return True


def make_client(
    activator: FakeActivator,
    bootstrap: KeyBootstrap | None,
) -> AsyncClient:
    store = MemoryStore()
    app = create_app(
        broker=InMemoryBroker(store),
        key_verifier=_NullVerifier(),
        key_bootstrap=bootstrap,
        key_activator=activator.activate,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _NullVerifier:
    async def verify(self, key: str | None) -> bool:
        return False


def test_format_joins_prefix_and_hash_with_one_colon() -> None:
    assert format_key_material(PREFIX, HASH) == f"{PREFIX}:{HASH}"


def test_parse_round_trips_format_output() -> None:
    material = format_key_material(PREFIX, HASH)
    assert parse_key_material(material) == (PREFIX, HASH)


def test_parse_accepts_real_argon2id_hash() -> None:
    digest = CHEAP_HASHER.hash("gmq_" + "k" * 43)
    material = format_key_material("gmq_zz99yy88", digest)
    assert parse_key_material(material) == ("gmq_zz99yy88", digest)


def test_parse_rejects_missing_colon() -> None:
    assert parse_key_material(f"{PREFIX}{HASH}") is None


def test_parse_rejects_second_colon() -> None:
    assert parse_key_material(f"{PREFIX}:{HASH}:extra") is None


def test_parse_rejects_empty_parts() -> None:
    assert parse_key_material(f":{HASH}") is None
    assert parse_key_material(f"{PREFIX}:") is None
    assert parse_key_material("") is None


def test_parse_rejects_bad_prefix() -> None:
    assert parse_key_material(f"notgmq_12345:{HASH}") is None
    assert parse_key_material(f"gmq_short:{HASH}") is None
    assert parse_key_material(f"gmq_ab12cd345:{HASH}") is None
    assert parse_key_material(f"gmq_ab12cd$5:{HASH}") is None


def test_parse_rejects_non_argon2id_hash() -> None:
    assert parse_key_material(f"{PREFIX}:sha256$abc") is None
    assert parse_key_material(f"{PREFIX}:argon2id$no-leading-dollar") is None


def test_parse_rejects_hash_longer_than_column_width() -> None:
    oversized = "$argon2id$" + "h" * 200
    assert parse_key_material(f"{PREFIX}:{oversized}") is None


async def test_endpoint_404_when_bootstrap_not_configured() -> None:
    activator = FakeActivator()
    async with make_client(activator, None) as client:
        response = await client.post("/bootstrap/api-key")
    assert response.status_code == 404
    assert activator.calls == 0


async def test_endpoint_404_when_disabled() -> None:
    activator = FakeActivator()
    bootstrap = KeyBootstrap(enabled=False, material=SecretStr(MATERIAL))
    async with make_client(activator, bootstrap) as client:
        response = await client.post("/bootstrap/api-key")
    assert response.status_code == 404
    assert activator.calls == 0


async def test_endpoint_201_activates_material_from_settings() -> None:
    activator = FakeActivator()
    bootstrap = KeyBootstrap(enabled=True, material=SecretStr(MATERIAL))
    async with make_client(activator, bootstrap) as client:
        response = await client.post("/bootstrap/api-key")

    assert response.status_code == 201
    assert response.json() == {"status": "activated", "prefix": PREFIX}
    assert activator.rows == {PREFIX: HASH}


async def test_endpoint_conflict_returns_409() -> None:
    activator = FakeActivator(exists={PREFIX})
    bootstrap = KeyBootstrap(enabled=True, material=SecretStr(MATERIAL))
    async with make_client(activator, bootstrap) as client:
        response = await client.post("/bootstrap/api-key")

    assert response.status_code == 409
    assert response.json() == {"detail": "api key already activated"}
    assert activator.rows == {}


async def test_endpoint_503_when_material_missing() -> None:
    activator = FakeActivator()
    bootstrap = KeyBootstrap(enabled=True, material=None)
    async with make_client(activator, bootstrap) as client:
        response = await client.post("/bootstrap/api-key")

    assert response.status_code == 503
    assert response.json() == {"detail": "bootstrap material missing or invalid"}
    assert activator.calls == 0


async def test_endpoint_503_when_material_invalid() -> None:
    activator = FakeActivator()
    bootstrap = KeyBootstrap(enabled=True, material=SecretStr("gmq_bad:xxx"))
    async with make_client(activator, bootstrap) as client:
        response = await client.post("/bootstrap/api-key")

    assert response.status_code == 503
    assert response.json() == {"detail": "bootstrap material missing or invalid"}
    assert activator.calls == 0


async def test_activated_key_passes_the_production_verifier() -> None:
    # US2 scenario 3 without a DB: the endpoint-inserted row feeds the
    # real ApiKeyVerifier, and the stage-1 plaintext authenticates.
    plaintext = "gmq_" + "v" * 43
    digest = CHEAP_HASHER.hash(plaintext)
    prefix = plaintext[:12]
    material = format_key_material(prefix, digest)

    activator = FakeActivator()
    bootstrap = KeyBootstrap(enabled=True, material=SecretStr(material))
    async with make_client(activator, bootstrap) as client:
        response = await client.post("/bootstrap/api-key")
    assert response.status_code == 201

    async def fetch(p: str) -> tuple[str, bool] | None:
        return (activator.rows[p], False) if p in activator.rows else None

    verifier = ApiKeyVerifier(fetch, CHEAP_HASHER)
    assert await verifier.verify(plaintext) is True
    assert await verifier.verify("gmq_" + "x" * 43) is False


async def test_repeat_activation_conflicts_without_mutation() -> None:
    activator = FakeActivator()
    bootstrap = KeyBootstrap(enabled=True, material=SecretStr(MATERIAL))
    async with make_client(activator, bootstrap) as client:
        first = await client.post("/bootstrap/api-key")
        second = await client.post("/bootstrap/api-key")

    assert first.status_code == 201
    assert second.status_code == 409
    assert activator.rows == {PREFIX: HASH}, "no duplicate row may appear"


async def test_concurrent_activations_leave_exactly_one_row() -> None:
    # PK semantics simulated by the fake activator: the second inserter
    # sees the prefix taken and reports a conflict (US3 scenario 2).
    activator = FakeActivator()
    bootstrap = KeyBootstrap(enabled=True, material=SecretStr(MATERIAL))
    async with make_client(activator, bootstrap) as client:
        responses = await asyncio.gather(
            client.post("/bootstrap/api-key"),
            client.post("/bootstrap/api-key"),
        )

    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409]
    assert activator.rows == {PREFIX: HASH}


async def test_no_response_ever_contains_hash_or_material() -> None:
    # US3 scenario 3: whatever the outcome, secrets stay out of responses.
    activator = FakeActivator(exists={PREFIX})
    bootstrap = KeyBootstrap(enabled=True, material=SecretStr(MATERIAL))
    async with make_client(activator, bootstrap) as client:
        responses = [
            await client.post("/bootstrap/api-key"),
            await client.post("/bootstrap/api-key"),
        ]
    for response in responses:
        body = response.text
        assert HASH not in body
        assert MATERIAL not in body
        assert "$argon2id$" not in body
