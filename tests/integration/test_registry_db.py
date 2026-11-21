"""DB-gated integration of the bots-table registry adapter (specs 002→006).

Runs only against the test database (Constitution, Testing):
GRAMMQ_TEST_DATABASE_URL / TEST_DATABASE_URL or ./.env.test — otherwise
the whole module is skipped. Never the deploy env-file.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gram_mq.adapters.postgres.bot_registry import SqlAlchemyBotRegistry
from gram_mq.settings import table_name
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"
)


async def _truncate_bots() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"TRUNCATE {table_name('messages')}, "
                f"{table_name('bot_claim_state')}, {table_name('bots')} CASCADE"
            )
        )
    await engine.dispose()


async def test_registry_add_contains_fetch_delete(_pg_schema: None) -> None:
    await _truncate_bots()
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        registry = SqlAlchemyBotRegistry(async_sessionmaker(engine))

        assert not await registry.contains("0010-a-b")
        assert await registry.fetch_half("0010-a-b") is None

        await registry.add("0010-a-b", token_half="deadbeef" * 8, pair_nonce="00" * 8)
        assert await registry.contains("0010-a-b")
        assert await registry.fetch_half("0010-a-b") == ("deadbeef" * 8, "00" * 8)

        assert await registry.delete("0010-a-b")
        assert not await registry.contains("0010-a-b")
        assert not await registry.delete("0010-a-b")
    finally:
        await engine.dispose()


async def test_registry_add_makes_bot_claimable(_pg_schema: None) -> None:
    """Regression (2026-10-11, kept through 006): a registered bot must
    appear in the claim fairness ledger — otherwise PostgresBroker.claim
    never sees its queued messages and the worker idles silently."""
    from gram_mq.adapters.postgres.broker import PostgresBroker
    from gram_mq.ports.broker import OutboundMessage

    await _truncate_bots()
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine)
        slug = "0010-claim-me"
        await SqlAlchemyBotRegistry(factory).add(
            slug, token_half="aa" * 16, pair_nonce="00" * 8
        )

        broker = PostgresBroker(database_url=TEST_DATABASE_URL, max_attempts=3)
        try:
            message = OutboundMessage(
                id=uuid4(), bot_slug=slug, chat_id="1", payload={"text": "x"}
            )
            await broker.enqueue(message)
            delivery = await broker.claim(worker_id="regression")
            assert delivery is not None
            assert delivery.id == message.id
            assert delivery.bot_slug == slug
        finally:
            await broker.aclose()
    finally:
        await engine.dispose()
