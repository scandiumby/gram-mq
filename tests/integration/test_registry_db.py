"""DB-gated integration of the wizard's registry path (spec 002, T013).

Runs only against the test database (Constitution, Testing):
GRAMMQ_TEST_DATABASE_URL / TEST_DATABASE_URL or ./.env.test — otherwise
the whole module is skipped. Never the deploy env-file.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gram_mq.cli.wizard import SqlAlchemyBotRegistry, run_bot_add
from gram_mq.models.bots import Bot
from gram_mq.settings import table_name
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"
)


async def _truncate_bots() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {table_name('bots')} CASCADE"))
    await engine.dispose()


async def test_registry_insert_and_contains(_pg_schema: None) -> None:
    await _truncate_bots()
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        registry = SqlAlchemyBotRegistry(factory)

        assert not await registry.contains("0010-a-b")
        await registry.add("0010-a-b")
        assert await registry.contains("0010-a-b")
    finally:
        await engine.dispose()


async def test_wizard_end_to_end_with_real_db(tmp_path: Path, _pg_schema: None) -> None:
    await _truncate_bots()
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        result = await run_bot_add(
            bots_dir=tmp_path / "bots",
            registry=SqlAlchemyBotRegistry(factory),
            input_fn=lambda _prompt: "Integration Bot",
            token_input_fn=lambda _prompt: "987654321:it-test-secret",
        )

        assert result.bot_slug.startswith("4321-")
        assert result.token_file.exists()

        async with factory() as session:
            row = await session.get(Bot, result.bot_slug)
            assert row is not None
            assert row.bot_slug == result.bot_slug
    finally:
        await engine.dispose()


async def test_registry_add_makes_bot_claimable(
    tmp_path: Path, _pg_schema: None
) -> None:
    """Regression (2026-10-11): a bot registered through the CLI registry
    must appear in the claim fairness ledger — otherwise PostgresBroker
    .claim never sees its queued messages and the worker idles silently."""
    from gram_mq.adapters.postgres.broker import PostgresBroker
    from gram_mq.ports.broker import OutboundMessage

    await _truncate_bots()
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        slug = "0010-claim-me"
        await SqlAlchemyBotRegistry(factory).add(slug)

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
