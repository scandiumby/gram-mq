"""DB-gated integration of the bot lifecycle (spec 006: US1..US3).

Runs only against the test database (Constitution, Testing):
GRAMMQ_TEST_DATABASE_URL / TEST_DATABASE_URL or ./.env.test — otherwise
the whole module is skipped. Never the deploy env-file.

Covers the whole journal in one flow: migration 0003 (NULL-able halves,
no messages FK), provisioning over the real registry, enqueue's explicit
registry check, split-storage glue through SplitFileBotConfig, and the
hard revoke with an occupied queue.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gram_mq.adapters.postgres.bot_registry import SqlAlchemyBotRegistry
from gram_mq.adapters.postgres.broker import PostgresBroker
from gram_mq.bot_admin import DuplicateTokenError, provision_bot, revoke_bot
from gram_mq.ports.broker import OutboundMessage, UnknownBotError
from gram_mq.settings import table_name
from gram_mq.token_store import bot_file_path, parse_master_key, read_bot_file
from gram_mq.worker.bot_config import (
    NotReady,
    NotRegistered,
    Ready,
    SplitFileBotConfig,
)
from gram_mq.worker.runner import process_delivery
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"
)

MASTER_HEX = os.urandom(32).hex()
TOKEN = "123456789:" + "a" * 32


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_text(self, *, bot_token: str, chat_id: str, text: str) -> str:
        self.sent.append((bot_token, chat_id, text))
        return "tg-1"


@pytest_asyncio.fixture
async def factory(
    _pg_schema: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"TRUNCATE {table_name('messages')}, {table_name('bots')}, "
                f"{table_name('bot_claim_state')} CASCADE"
            )
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def bots_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bots"
    directory.mkdir(mode=0o700)
    return directory


def registry(factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyBotRegistry:
    return SqlAlchemyBotRegistry(factory)


async def test_unknown_bot_enqueue_is_unknown_bot_error_without_fk(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Migration 0003 dropped the messages->bots FK; the explicit registry
    check in enqueue keeps the UnknownBotError contract."""
    broker = PostgresBroker(database_url=TEST_DATABASE_URL)
    try:
        with pytest.raises(UnknownBotError):
            await broker.enqueue(
                OutboundMessage(
                    id=uuid.uuid4(),
                    bot_slug="0000-never-registered",
                    chat_id="42",
                    payload={"text": "hi"},
                )
            )
    finally:
        await broker.aclose()


async def test_provision_enqueue_deliver_glue_over_db(
    factory: async_sessionmaker[AsyncSession],
    bots_dir: Path,
) -> None:
    key = parse_master_key(SecretStr(MASTER_HEX))
    assert key is not None
    slug = await provision_bot(
        bots_dir=bots_dir,
        registry=registry(factory),
        master_key=key,
        token=TOKEN,
        name="Integration Bot",
    )

    # Registry row + claim-state seeding + encrypted file half, no plaintext.
    async with factory() as session:
        bots = await session.execute(text(f"SELECT bot_slug FROM {table_name('bots')}"))
        claims = await session.execute(
            text(f"SELECT bot_slug FROM {table_name('bot_claim_state')}")
        )
        assert bots.scalars().all() == [slug]
        assert claims.scalars().all() == [slug]
    record = read_bot_file(bot_file_path(bots_dir, slug))
    assert record is not None
    assert TOKEN not in bot_file_path(bots_dir, slug).read_text(encoding="utf-8")

    # Same token again: the live bot blocks re-registration (409 source).
    with pytest.raises(DuplicateTokenError):
        await provision_bot(
            bots_dir=bots_dir,
            registry=registry(factory),
            master_key=key,
            token=TOKEN,
            name="Again",
        )

    # Full delivery path: enqueue -> claim -> split-storage glue -> send.
    broker = PostgresBroker(database_url=TEST_DATABASE_URL)
    config = SplitFileBotConfig(
        bots_dir=bots_dir, fetch_half=registry(factory).fetch_half, master_key=key
    )
    sender = FakeSender()
    try:
        await broker.enqueue(
            OutboundMessage(
                id=uuid.uuid4(),
                bot_slug=slug,
                chat_id="42",
                payload={"text": "hello"},
            )
        )
        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None
        assert await config.token_for(slug) == Ready(TOKEN)

        await process_delivery(
            delivery,
            broker=broker,
            sender=sender,
            bot_config=config,
            backoff_base=5.0,
            max_attempts=3,
        )
    finally:
        await broker.aclose()
    assert sender.sent == [(TOKEN, "42", "hello")]
    async with factory() as session:
        rows = await session.execute(
            text(f"SELECT status, telegram_message_id FROM {table_name('messages')}")
        )
        assert rows.all() == [("sent", "tg-1")]


async def test_legacy_null_halves_row_is_not_ready(
    factory: async_sessionmaker[AsyncSession],
    bots_dir: Path,
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                f"INSERT INTO {table_name('bots')} (bot_slug, created_at) "
                f"VALUES ('6789-old-bot', now())"
            )
        )
        await session.commit()
    config = SplitFileBotConfig(
        bots_dir=bots_dir,
        fetch_half=registry(factory).fetch_half,
        master_key=b"\x00" * 32,
    )
    assert await config.token_for("6789-old-bot") == NotReady()


async def test_revoke_dead_letters_queue_and_frees_token_id(
    factory: async_sessionmaker[AsyncSession],
    bots_dir: Path,
) -> None:
    key = parse_master_key(SecretStr(MASTER_HEX))
    assert key is not None
    bot_registry = registry(factory)
    slug = await provision_bot(
        bots_dir=bots_dir,
        registry=bot_registry,
        master_key=key,
        token=TOKEN,
        name="Revoke Me",
    )

    broker = PostgresBroker(database_url=TEST_DATABASE_URL)
    config = SplitFileBotConfig(
        bots_dir=bots_dir, fetch_half=bot_registry.fetch_half, master_key=key
    )
    message_id = uuid.uuid4()
    try:
        await broker.enqueue(
            OutboundMessage(
                id=message_id,
                bot_slug=slug,
                chat_id="42",
                payload={"text": "last words"},
            )
        )
        assert await revoke_bot(bots_dir=bots_dir, registry=bot_registry, bot_slug=slug)

        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None
        assert delivery.id == message_id
        await process_delivery(
            delivery,
            broker=broker,
            sender=FakeSender(),
            bot_config=config,
            backoff_base=5.0,
            max_attempts=3,
        )

        async with factory() as session:
            rows = await session.execute(
                text(
                    f"SELECT status, error FROM {table_name('messages')} "
                    f"WHERE id = '{message_id}'"
                )
            )
            assert rows.all() == [("failed", "bot not registered")]
            bots_left = await session.execute(
                text(f"SELECT count(*) FROM {table_name('bots')}")
            )
            claims_left = await session.execute(
                text(f"SELECT count(*) FROM {table_name('bot_claim_state')}")
            )
            assert bots_left.scalar_one() == 0
            # The ledger row deliberately survives the bot (the claim
            # round-robin iterates it) — the price of immediate
            # dead-lettering without journal surgery.
            assert claims_left.scalar_one() == 1

        # The queue rejects new messages for the revoked slug, and a
        # second revoke is a clean False (HTTP 404 source).
        with pytest.raises(UnknownBotError):
            await broker.enqueue(
                OutboundMessage(
                    id=uuid.uuid4(),
                    bot_slug=slug,
                    chat_id="42",
                    payload={"text": "nope"},
                )
            )
        assert not await revoke_bot(
            bots_dir=bots_dir, registry=bot_registry, bot_slug=slug
        )
        # The freed token id registers again without a conflict.
        assert await config.token_for(slug) == NotRegistered()
        await provision_bot(
            bots_dir=bots_dir,
            registry=bot_registry,
            master_key=key,
            token=TOKEN,
            name="Reborn",
        )
    finally:
        await broker.aclose()
    assert not bot_file_path(bots_dir, slug).exists()
