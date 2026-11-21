"""DB-gated integration of the inbound flow (spec 007: US1, FR-003/012).

Runs only against the test database (Constitution, Testing):
GRAMMQ_TEST_DATABASE_URL / TEST_DATABASE_URL or ./.env.test — otherwise
the whole module is skipped. Never the deploy env-file.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gram_mq.adapters.postgres.inbound_broker import PostgresInboundBroker
from gram_mq.adapters.postgres.webhook_auth import SqlAlchemyWebhookAuth
from gram_mq.api.app import create_app
from gram_mq.models.bots import Bot
from gram_mq.ports.broker import Delivery, OutboundMessage, UnknownBotError
from gram_mq.ports.inbound import InboundUpdate
from gram_mq.settings import table_name
from gram_mq.webhook_store import webhook_secret_hmac
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"
)

SLUG = "0070-int-caladan"
HEADER = "X-Telegram-Bot-Api-Secret-Token"
INBOUND_TABLE = table_name("inbound_updates")
LEDGER_TABLE = table_name("bot_inbound_claim_state")
BOTS_TABLE = table_name("bots")


class _OutboundStub:
    """A no-op BrokerPort: this module never touches the outbound side."""

    async def enqueue(self, message: OutboundMessage) -> None:
        return None

    async def claim(self, *, worker_id: str) -> Delivery | None:
        return None

    async def ack(self, delivery: Delivery) -> None:
        return None

    async def retry(
        self,
        delivery: Delivery,
        delay: timedelta,
        reason: str,
        *,
        count_attempt: bool = True,
    ) -> None:
        return None

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        return None

    async def queue_depth(self, bot_slug: str) -> int:
        return 0


class _KeyStub:
    async def verify(self, key: str | None) -> bool:
        return True


@pytest_asyncio.fixture
async def session_factory(
    _pg_schema: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE {INBOUND_TABLE}, {LEDGER_TABLE}, {BOTS_TABLE} CASCADE")
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def make_update(update_id: int = 1, text: str = "hi") -> InboundUpdate:
    from uuid import uuid4

    return InboundUpdate(
        id=uuid4(),
        bot_slug=SLUG,
        update_id=update_id,
        payload={"update_id": update_id, "message": {"text": text}},
    )


class TestMigration:
    async def test_objects_exist(self) -> None:
        engine = create_async_engine(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            tables = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
            columns = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{BOTS_TABLE}'"
                )
            )
        await engine.dispose()
        names = {row[0] for row in tables}
        assert INBOUND_TABLE in names
        assert LEDGER_TABLE in names
        assert "webhook_secret_hmac" in {row[0] for row in columns}


class TestBrokerOnPostgres:
    async def test_dedup_semantics_on_jsonb(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        broker = PostgresInboundBroker(database_url=TEST_DATABASE_URL, max_attempts=3)
        await broker.register_bot(SLUG)

        assert await broker.enqueue_inbound(make_update(7, "same")) is True
        assert await broker.enqueue_inbound(make_update(7, "same")) is False
        assert await broker.enqueue_inbound(make_update(7, "different")) is True

        assert await broker.queue_depth(SLUG) == 2
        await broker.aclose()

    async def test_revoked_bot_rejects_new_journal_survives(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        broker = PostgresInboundBroker(database_url=TEST_DATABASE_URL, max_attempts=3)
        await broker.register_bot(SLUG)
        await broker.enqueue_inbound(make_update(1))

        async with session_factory() as session:
            await session.execute(delete(Bot).where(Bot.bot_slug == SLUG))
            await session.commit()

        with pytest.raises(UnknownBotError):
            await broker.enqueue_inbound(make_update(2))
        assert await broker.queue_depth(SLUG) == 1
        await broker.aclose()


class TestEndpointEndToEnd:
    async def test_full_webhook_path_over_postgres(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        master_key = secrets.token_bytes(32)
        secret = secrets.token_urlsafe(32)
        async with session_factory() as session:
            session.add(
                Bot(
                    bot_slug=SLUG,
                    token_half=None,
                    pair_nonce=None,
                    webhook_secret_hmac=webhook_secret_hmac(master_key, secret),
                )
            )
            await session.commit()

        broker = PostgresInboundBroker(database_url=TEST_DATABASE_URL, max_attempts=3)
        auth = SqlAlchemyWebhookAuth(session_factory, master_key)
        app = create_app(
            broker=_OutboundStub(),
            key_verifier=_KeyStub(),
            inbound_broker=broker,
            webhook_auth=auth,
        )

        payload = {"update_id": 42, "message": {"text": "hi"}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            ok = await client.post(
                f"/webhooks/{SLUG}", json=payload, headers={HEADER: secret}
            )
            bad = await client.post(
                f"/webhooks/{SLUG}", json=payload, headers={HEADER: "forged"}
            )

        assert ok.status_code == 200
        assert bad.status_code == 401

        engine = create_async_engine(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    f"SELECT status, update_id FROM {INBOUND_TABLE} "
                    f"WHERE bot_slug = :slug"
                ),
                {"slug": SLUG},
            )
        await engine.dispose()
        journaled = rows.all()
        assert len(journaled) == 1  # the forged request wrote nothing
        assert journaled[0][0] == "queued"
        assert journaled[0][1] == 42
        await broker.aclose()
