"""DB-gated integration of the API and the worker (spec 003: US4, SC-001/002).

Runs only against the test database (Constitution, Testing):
GRAMMQ_TEST_DATABASE_URL / TEST_DATABASE_URL or ./.env.test — otherwise
the whole module is skipped. Never the deploy env-file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gram_mq.adapters.postgres.broker import PostgresBroker
from gram_mq.api.app import create_app
from gram_mq.api.auth import ApiKeyVerifier, hash_key
from gram_mq.ports.telegram import TelegramSender
from gram_mq.settings import table_name
from gram_mq.worker.bot_config import BotConfig
from gram_mq.worker.runner import process_delivery
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"
)

SLUG = "0010-int-a"
API_KEY = "gmq_" + "i" * 43

# The quickstart §5 counting query, verbatim.
COUNT_SENT_SQL = f"SELECT count(*) FROM {table_name('messages')} WHERE status = 'sent'"


class FixedKeySender(TelegramSender):
    """Fake sender: always succeeds, ids tg-1, tg-2, ..."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_text(self, *, bot_token: str, chat_id: str, text: str) -> str:
        self.calls += 1
        return f"tg-{self.calls}"


class StaticBotConfig(BotConfig):
    def __init__(self, token: str) -> None:
        self.token = token

    async def token_for(self, bot_slug: str) -> str | None:
        return self.token


async def _truncate() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        tables = ", ".join(
            table_name(name)
            for name in ("messages", "bot_claim_state", "bots", "api_keys")
        )
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    await engine.dispose()


@pytest_asyncio.fixture
async def factory(
    _pg_schema: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    await _truncate()
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def broker(
    factory: async_sessionmaker[AsyncSession],
) -> PostgresBroker:
    broker = PostgresBroker(
        database_url=TEST_DATABASE_URL, lease_seconds=60, max_attempts=3
    )
    await broker.register_bot(SLUG)
    return broker


@pytest_asyncio.fixture
async def client(
    broker: PostgresBroker, factory: async_sessionmaker[AsyncSession]
) -> AsyncClient:
    digest = hash_key(API_KEY)

    async def fetch(prefix: str) -> tuple[str, bool] | None:
        async with factory() as session:
            from gram_mq.models.api_keys import ApiKey

            row = await session.get(ApiKey, prefix)
            if row is None:
                return None
            return row.key_hash, row.revoked_at is not None

    async with factory() as session:
        from gram_mq.models.api_keys import ApiKey

        async with session.begin():
            session.add(ApiKey(prefix=API_KEY[:12], key_hash=digest))

    app = create_app(broker=broker, key_verifier=ApiKeyVerifier(fetch))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_post_creates_queued_row(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(
        "/api/v1/messages",
        json={"bot_slug": SLUG, "chat_id": "123456", "text": "integration"},
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 202
    async with factory() as session:
        count = await session.execute(
            text(
                f"SELECT count(*) FROM {table_name('messages')} WHERE status = 'queued'"
            )
        )
        assert count.scalar_one() == 1


async def test_rejections_do_not_touch_the_journal(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    await client.post("/api/v1/messages", json={})
    await client.post(
        "/api/v1/messages",
        json={"bot_slug": "no-such", "chat_id": "1", "text": "x"},
        headers={"X-API-Key": "gmq_" + "z" * 43},
    )

    async with factory() as session:
        count = await session.execute(
            text(f"SELECT count(*) FROM {table_name('messages')}")
        )
        assert count.scalar_one() == 0


async def test_sc002_count_sent_matches_deliveries(
    client: AsyncClient,
    broker: PostgresBroker,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    for index in range(3):
        response = await client.post(
            "/api/v1/messages",
            json={"bot_slug": SLUG, "chat_id": "123456", "text": f"m{index}"},
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 202

    sender = FixedKeySender()
    bot_config = StaticBotConfig("10:integration-secret")
    while True:
        delivery = await broker.claim(worker_id="int-test")
        if delivery is None:
            break
        await process_delivery(
            delivery,
            broker=broker,
            sender=sender,
            bot_config=bot_config,
            backoff_base=5.0,
            max_attempts=3,
        )

    assert sender.calls == 3
    async with factory() as session:
        sent = await session.execute(text(COUNT_SENT_SQL))
        assert sent.scalar_one() == 3
