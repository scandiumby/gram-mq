"""Unit contract of the inbound API (spec 003: US2, FR-002..FR-005, FR-010).

Memory broker + fake verifier: the whole surface without infrastructure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gram_mq.adapters.memory.broker import InMemoryBroker, MemoryStore
from gram_mq.api.app import create_app

SLUG = "0010-arrakis-melange"
VALID_KEY = "gmq_" + "v" * 43
WRONG_KEY = "gmq_" + "x" * 43


class FakeVerifier:
    def __init__(self, valid_keys: set[str]) -> None:
        self.valid_keys = valid_keys

    async def verify(self, key: str | None) -> bool:
        return key in self.valid_keys


@pytest_asyncio.fixture
async def broker() -> InMemoryBroker:
    broker = InMemoryBroker(
        MemoryStore(), now=lambda: datetime.now(UTC), max_attempts=3
    )
    await broker.register_bot(SLUG)
    return broker


def make_client(broker: InMemoryBroker, valid_keys: set[str]) -> AsyncClient:
    app = create_app(broker=broker, key_verifier=FakeVerifier(valid_keys))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def body(**overrides: str) -> dict[str, str]:
    payload = {"bot_slug": SLUG, "chat_id": "123456", "text": "hello"}
    payload.update(overrides)
    return payload


async def test_valid_post_returns_202_with_id(broker: InMemoryBroker) -> None:
    async with make_client(broker, {VALID_KEY}) as client:
        response = await client.post(
            "/api/v1/messages", json=body(), headers={"X-API-Key": VALID_KEY}
        )

    assert response.status_code == 202
    UUID(response.json()["id"])  # parses as a uuid
    assert await broker.queue_depth(SLUG) == 1


async def test_missing_key_is_401_without_journal_change(
    broker: InMemoryBroker,
) -> None:
    async with make_client(broker, {VALID_KEY}) as client:
        response = await client.post("/api/v1/messages", json=body())

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert await broker.queue_depth(SLUG) == 0


async def test_wrong_key_is_the_same_401(broker: InMemoryBroker) -> None:
    async with make_client(broker, {VALID_KEY}) as client:
        response = await client.post(
            "/api/v1/messages", json=body(), headers={"X-API-Key": WRONG_KEY}
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert VALID_KEY not in response.text
    assert await broker.queue_depth(SLUG) == 0


async def test_revoked_key_is_the_same_401(broker: InMemoryBroker) -> None:
    async with make_client(broker, set()) as client:
        response = await client.post(
            "/api/v1/messages", json=body(), headers={"X-API-Key": VALID_KEY}
        )

    assert response.status_code == 401


async def test_key_in_query_parameter_is_not_authorization(
    broker: InMemoryBroker,
) -> None:
    async with make_client(broker, {VALID_KEY}) as client:
        response = await client.post(
            f"/api/v1/messages?X-API-Key={VALID_KEY}", json=body()
        )

    assert response.status_code == 401
    assert await broker.queue_depth(SLUG) == 0


async def test_unknown_bot_is_404(broker: InMemoryBroker) -> None:
    async with make_client(broker, {VALID_KEY}) as client:
        response = await client.post(
            "/api/v1/messages",
            json=body(bot_slug="no-such-bot"),
            headers={"X-API-Key": VALID_KEY},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "bot not found"}


@pytest.mark.parametrize(
    "payload",
    [
        {"bot_slug": "", "chat_id": "123456", "text": "hello"},
        {"bot_slug": SLUG, "chat_id": "123456", "text": ""},
        {"bot_slug": SLUG, "chat_id": "123456", "text": "x" * 4097},
        {"bot_slug": SLUG, "chat_id": "abc", "text": "hello"},
        {"bot_slug": SLUG, "chat_id": "", "text": "hello"},
        {"chat_id": "123456", "text": "hello"},
    ],
)
async def test_invalid_bodies_are_422(
    broker: InMemoryBroker, payload: dict[str, str]
) -> None:
    async with make_client(broker, {VALID_KEY}) as client:
        response = await client.post(
            "/api/v1/messages", json=payload, headers={"X-API-Key": VALID_KEY}
        )

    assert response.status_code == 422
    assert await broker.queue_depth(SLUG) == 0


async def test_health_needs_no_key(broker: InMemoryBroker) -> None:
    async with make_client(broker, set()) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_http_bot_registration_does_not_exist(
    broker: InMemoryBroker,
) -> None:
    async with make_client(broker, {VALID_KEY}) as client:
        post = await client.post(
            "/api/v1/bots",
            json={"bot_slug": "0010-x"},
            headers={"X-API-Key": VALID_KEY},
        )
        put = await client.put(
            "/api/v1/messages", json=body(), headers={"X-API-Key": VALID_KEY}
        )

    assert post.status_code == 404
    assert put.status_code == 405
