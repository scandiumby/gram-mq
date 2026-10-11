"""Unit contract of the delivery worker (spec 003: US3; Constitution II).

Memory broker with an injectable clock keeps lease expiry, backoff and
max_attempts deterministic. SC-003 (crash between send and ack) is
reproduced exactly: the send happens, the ack does not.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from gram_mq.adapters.memory.broker import InMemoryBroker, MemoryStore
from gram_mq.ports.broker import OutboundMessage
from gram_mq.ports.telegram import TelegramSendError
from gram_mq.worker.runner import process_delivery, run_worker
from tests.conftest import FakeClock

logging.disable(logging.WARNING)

SLUG = "0010-a-b"
TOKEN = "10:twenty"
CHAT = "42"


class FakeSender:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.calls = 0
        self.fail = fail

    async def send_text(self, *, bot_token: str, chat_id: str, text: str) -> str:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        self.sent.append((bot_token, chat_id, text))
        return f"tg-{self.calls}"


class FakeBotConfig:
    def __init__(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens

    async def token_for(self, bot_slug: str) -> str | None:
        return self.tokens.get(bot_slug)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def broker(clock: FakeClock) -> InMemoryBroker:
    broker = InMemoryBroker(
        MemoryStore(), now=clock.now, lease_seconds=60, max_attempts=3
    )
    await broker.register_bot(SLUG)
    return broker


async def enqueue(broker: InMemoryBroker) -> None:
    await broker.enqueue(
        OutboundMessage(id=uuid4(), bot_slug=SLUG, chat_id=CHAT, payload={"text": "hi"})
    )


def config(tokens: dict[str, str] | None = None) -> FakeBotConfig:
    return FakeBotConfig(tokens if tokens is not None else {SLUG: TOKEN})


async def test_happy_path_acks_atomically(
    broker: InMemoryBroker, clock: FakeClock
) -> None:
    await enqueue(broker)
    sender = FakeSender()

    delivery = await broker.claim(worker_id="w1")
    assert delivery is not None
    await process_delivery(
        delivery,
        broker=broker,
        sender=sender,
        bot_config=config(),
        backoff_base=5.0,
        max_attempts=3,
    )

    assert sender.sent == [(TOKEN, CHAT, "hi")]
    assert await broker.queue_depth(SLUG) == 0
    # Test-double internals: assert the terminal state and the atomic
    # telegram_message_id written by ack.
    row = broker._store.rows[0]
    assert row.status == "sent"
    assert row.telegram_message_id == "tg-1"
    assert row.sent_at is not None


async def test_retryable_error_requeues_with_backoff(
    broker: InMemoryBroker, clock: FakeClock
) -> None:
    await enqueue(broker)
    sender = FakeSender(fail=TelegramSendError("telegram network error"))

    delivery = await broker.claim(worker_id="w1")
    assert delivery is not None
    await process_delivery(
        delivery,
        broker=broker,
        sender=sender,
        bot_config=config(),
        backoff_base=5.0,
        max_attempts=3,
    )

    assert await broker.queue_depth(SLUG) == 1
    row = broker._store.rows[0]
    assert row.attempts == 1
    assert row.available_at == clock.now_value + timedelta(seconds=5)
    assert "network" in (row.error or "")

    # Not claimable before the backoff elapses, claimable after.
    clock.advance(timedelta(seconds=4))
    assert await broker.claim(worker_id="w1") is None
    clock.advance(timedelta(seconds=1))
    reclaimed = await broker.claim(worker_id="w1")
    assert reclaimed is not None
    assert reclaimed.context["attempts"] == 1


async def test_rate_limit_respects_retry_after(
    broker: InMemoryBroker, clock: FakeClock
) -> None:
    await enqueue(broker)
    sender = FakeSender(fail=TelegramSendError("rate limit", retry_after=30.0))

    delivery = await broker.claim(worker_id="w1")
    assert delivery is not None
    await process_delivery(
        delivery,
        broker=broker,
        sender=sender,
        bot_config=config(),
        backoff_base=5.0,
        max_attempts=3,
    )

    clock.advance(timedelta(seconds=29))
    assert await broker.claim(worker_id="w1") is None
    clock.advance(timedelta(seconds=1))
    assert await broker.claim(worker_id="w1") is not None


async def test_permanent_error_dead_letters(
    broker: InMemoryBroker, clock: FakeClock
) -> None:
    await enqueue(broker)
    sender = FakeSender(
        fail=TelegramSendError(
            "telegram rejected the message: TelegramBadRequest",
            permanent=True,
        )
    )

    delivery = await broker.claim(worker_id="w1")
    assert delivery is not None
    await process_delivery(
        delivery,
        broker=broker,
        sender=sender,
        bot_config=config(),
        backoff_base=5.0,
        max_attempts=3,
    )

    assert await broker.queue_depth(SLUG) == 0
    row = broker._store.rows[0]
    assert row.status == "failed"
    assert "TelegramBadRequest" in (row.error or "")


async def test_missing_token_file_retries(
    broker: InMemoryBroker, clock: FakeClock
) -> None:
    await enqueue(broker)
    sender = FakeSender()

    delivery = await broker.claim(worker_id="w1")
    assert delivery is not None
    await process_delivery(
        delivery,
        broker=broker,
        sender=sender,
        bot_config=config(tokens={}),  # no file for the bot
        backoff_base=5.0,
        max_attempts=3,
    )

    assert sender.calls == 0
    assert await broker.queue_depth(SLUG) == 1
    assert "token" in (broker._store.rows[0].error or "")


async def test_retry_exhaustion_fails(broker: InMemoryBroker, clock: FakeClock) -> None:
    await enqueue(broker)
    sender = FakeSender(fail=TelegramSendError("telegram network error"))

    for attempt in range(3):
        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None
        assert delivery.context["attempts"] == attempt
        await process_delivery(
            delivery,
            broker=broker,
            sender=sender,
            bot_config=config(),
            backoff_base=5.0,
            max_attempts=3,
        )
        clock.advance(timedelta(seconds=400))

    row = broker._store.rows[0]
    assert row.status == "failed"
    assert "max attempts" in (row.error or "")
    assert await broker.claim(worker_id="w1") is None


async def test_crash_between_send_and_ack_redelivers(
    broker: InMemoryBroker, clock: FakeClock
) -> None:
    """SC-003: no message is lost; a duplicate in the chat is acceptable."""
    await enqueue(broker)
    sender = FakeSender()

    delivery = await broker.claim(worker_id="w1")
    assert delivery is not None
    # Delivered to Telegram, but the worker dies before the ack.
    await sender.send_text(
        bot_token=TOKEN, chat_id=CHAT, text=str(delivery.payload["text"])
    )
    assert len(sender.sent) == 1

    clock.advance(timedelta(seconds=61))  # lease expires
    reclaimed = await broker.claim(worker_id="w2")
    assert reclaimed is not None
    assert reclaimed.id == delivery.id

    await process_delivery(
        reclaimed,
        broker=broker,
        sender=sender,
        bot_config=config(),
        backoff_base=5.0,
        max_attempts=3,
    )

    assert len(sender.sent) == 2  # the documented at-least-once duplicate
    assert broker._store.rows[0].status == "sent"


async def test_run_worker_processes_until_idle(
    broker: InMemoryBroker,
) -> None:
    await enqueue(broker)
    sender = FakeSender()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await run_worker(
                broker=broker,
                sender=sender,
                bot_config=config(),
                poll_interval=0.01,
                backoff_base=5.0,
                worker_id="w",
                max_attempts=3,
            )

    assert sender.calls == 1
    assert broker._store.rows[0].status == "sent"


async def test_rate_limit_never_burns_attempts(
    broker: InMemoryBroker, clock: FakeClock
) -> None:
    """2026-10-11 decision: a 429 pause is not a failed attempt — the
    message cannot reach failed through rate limits alone, even with
    max_attempts=2."""
    await enqueue(broker)
    sender = FakeSender(fail=TelegramSendError("telegram rate limit", retry_after=30.0))

    for _cycle in range(4):
        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None
        assert delivery.context["attempts"] == 0
        await process_delivery(
            delivery,
            broker=broker,
            sender=sender,
            bot_config=config(),
            backoff_base=5.0,
            max_attempts=2,
        )
        clock.advance(timedelta(seconds=31))

    row = broker._store.rows[0]
    assert row.status == "queued"
    assert row.attempts == 0
