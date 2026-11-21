"""The single contract suite for InboundBrokerPort (spec 007).

Runs unchanged against InMemoryInboundBroker (always) and
PostgresInboundBroker (when TEST_DATABASE_URL is set) via the fixtures in
tests/conftest.py. Covers the at-least-once mechanics mirrored from
feature 001 plus the R13 ingest idempotency: an identical retry is
absorbed, the same update_id with different content is a new update.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

import pytest

from gram_mq.ports.broker import IllegalDeliveryStateError, UnknownBotError
from gram_mq.ports.inbound import InboundUpdate
from tests.conftest import FakeClock, SeededInboundBroker


def make_update(
    bot_slug: str = "bot-a",
    update_id: int = 1,
    text: str = "hi",
) -> InboundUpdate:
    return InboundUpdate(
        id=uuid4(),
        bot_slug=bot_slug,
        update_id=update_id,
        payload={"update_id": update_id, "message": {"text": text}},
    )


class TestEnqueueInbound:
    async def test_new_update_journaled_counted_in_depth(
        self, inbound_broker: SeededInboundBroker
    ) -> None:
        await inbound_broker.register_bot("bot-a")

        assert await inbound_broker.enqueue_inbound(make_update()) is True
        assert await inbound_broker.queue_depth("bot-a") == 1

    async def test_unknown_bot_rejected(
        self, inbound_broker: SeededInboundBroker
    ) -> None:
        with pytest.raises(UnknownBotError):
            await inbound_broker.enqueue_inbound(make_update(bot_slug="ghost"))

    async def test_identical_retry_absorbed(
        self, inbound_broker: SeededInboundBroker
    ) -> None:
        await inbound_broker.register_bot("bot-a")
        update = make_update(update_id=7, text="same")

        assert await inbound_broker.enqueue_inbound(update) is True
        # A byte-identical Telegram retry (even with a fresh row id): no
        # new journal row, the caller still owes Telegram a 200 (R13).
        assert (
            await inbound_broker.enqueue_inbound(make_update(update_id=7, text="same"))
            is False
        )
        assert await inbound_broker.queue_depth("bot-a") == 1

    async def test_same_update_id_new_payload_is_new_update(
        self, inbound_broker: SeededInboundBroker
    ) -> None:
        await inbound_broker.register_bot("bot-a")

        assert (
            await inbound_broker.enqueue_inbound(make_update(update_id=7, text="old"))
            is True
        )
        # Telegram re-picks update_id randomly after a week of silence —
        # different content with the same id must not be swallowed.
        assert (
            await inbound_broker.enqueue_inbound(make_update(update_id=7, text="new"))
            is True
        )
        assert await inbound_broker.queue_depth("bot-a") == 2

    async def test_journal_survives_broker_recreation(
        self, make_inbound_broker: Callable[..., SeededInboundBroker]
    ) -> None:
        first = make_inbound_broker()
        await first.register_bot("bot-a")
        update = make_update()
        await first.enqueue_inbound(update)

        second = make_inbound_broker()
        delivery = await second.claim(worker_id="w2")

        assert delivery is not None
        assert delivery.id == update.id


class TestClaim:
    async def test_claim_leases_row_with_owner_and_deadline(
        self, inbound_broker: SeededInboundBroker
    ) -> None:
        await inbound_broker.register_bot("bot-a")
        await inbound_broker.enqueue_inbound(make_update(update_id=5))

        delivery = await inbound_broker.claim(worker_id="w1")

        assert delivery is not None
        assert delivery.bot_slug == "bot-a"
        assert delivery.update_id == 5
        assert delivery.payload["message"]["text"] == "hi"
        assert delivery.context["attempts"] == 0
        # Leased now: nothing else is claimable.
        assert await inbound_broker.claim(worker_id="w2") is None

    async def test_empty_queue_returns_none(
        self, inbound_broker: SeededInboundBroker
    ) -> None:
        await inbound_broker.register_bot("bot-a")

        assert await inbound_broker.claim(worker_id="w1") is None

    async def test_fairness_no_starvation(
        self,
        adapter: str,
        make_inbound_broker: Callable[..., SeededInboundBroker],
    ) -> None:
        broker = make_inbound_broker()
        for slug in ("bot-a", "bot-b", "bot-c"):
            await broker.register_bot(slug)

        bulk = 1000 if adapter == "memory" else 200
        for i in range(bulk):
            await broker.enqueue_inbound(make_update(bot_slug="bot-a", update_id=i))
        await broker.enqueue_inbound(make_update(bot_slug="bot-b", update_id=1))
        await broker.enqueue_inbound(make_update(bot_slug="bot-c", update_id=1))

        claimed_bots = []
        for _ in range(10):
            delivery = await broker.claim(worker_id="w1")
            if delivery is not None:
                claimed_bots.append(delivery.bot_slug)

        assert len(set(claimed_bots)) >= 2


class TestLeaseExpiry:
    async def test_expired_lease_reclaimed_by_other_worker(
        self,
        adapter: str,
        make_inbound_broker: Callable[..., SeededInboundBroker],
        clock: FakeClock,
    ) -> None:
        broker = make_inbound_broker(lease_seconds=0.2)
        await broker.register_bot("bot-a")
        update = make_update()
        await broker.enqueue_inbound(update)

        first = await broker.claim(worker_id="w1")
        assert first is not None

        if adapter == "memory":
            clock.advance(timedelta(seconds=0.3))
        else:
            await asyncio.sleep(0.3)

        second = await broker.claim(worker_id="w2")
        assert second is not None
        assert second.id == update.id

    async def test_live_lease_not_reclaimed(
        self,
        adapter: str,
        make_inbound_broker: Callable[..., SeededInboundBroker],
        clock: FakeClock,
    ) -> None:
        broker = make_inbound_broker(lease_seconds=60.0)
        await broker.register_bot("bot-a")
        await broker.enqueue_inbound(make_update())

        assert await broker.claim(worker_id="w1") is not None

        if adapter == "memory":
            clock.advance(timedelta(seconds=1))
        else:
            await asyncio.sleep(0.05)

        assert await broker.claim(worker_id="w2") is None


class TestOutcome:
    async def test_ack_records_delivered_atomically(
        self, inbound_broker: SeededInboundBroker
    ) -> None:
        await inbound_broker.register_bot("bot-a")
        await inbound_broker.enqueue_inbound(make_update())
        delivery = await inbound_broker.claim(worker_id="w1")
        assert delivery is not None

        await inbound_broker.ack(delivery)

        assert await inbound_broker.queue_depth("bot-a") == 0
        with pytest.raises(IllegalDeliveryStateError):
            await inbound_broker.ack(delivery)

    async def test_retry_enforces_backoff_then_returns(
        self,
        adapter: str,
        make_inbound_broker: Callable[..., SeededInboundBroker],
        clock: FakeClock,
    ) -> None:
        broker = make_inbound_broker()
        await broker.register_bot("bot-a")
        await broker.enqueue_inbound(make_update())
        first = await broker.claim(worker_id="w1")
        assert first is not None

        await broker.retry(first, delay=timedelta(seconds=0.2), reason="server down")
        assert await broker.claim(worker_id="w2") is None

        if adapter == "memory":
            clock.advance(timedelta(seconds=0.25))
        else:
            await asyncio.sleep(0.25)

        second = await broker.claim(worker_id="w2")
        assert second is not None
        assert second.context["attempts"] == 1

    async def test_retry_without_attempt_count_keeps_counter(
        self,
        adapter: str,
        make_inbound_broker: Callable[..., SeededInboundBroker],
        clock: FakeClock,
    ) -> None:
        broker = make_inbound_broker()
        await broker.register_bot("bot-a")
        await broker.enqueue_inbound(make_update())
        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None

        # 429 pause: waiting for retry_after is not a failed attempt.
        await broker.retry(
            delivery,
            delay=timedelta(seconds=0.15),
            reason="rate limited",
            count_attempt=False,
        )

        if adapter == "memory":
            clock.advance(timedelta(seconds=0.2))
        else:
            await asyncio.sleep(0.2)

        again = await broker.claim(worker_id="w2")
        assert again is not None
        assert again.context["attempts"] == 0

    async def test_dead_letter_is_terminal(
        self, inbound_broker: SeededInboundBroker
    ) -> None:
        await inbound_broker.register_bot("bot-a")
        await inbound_broker.enqueue_inbound(make_update())
        delivery = await inbound_broker.claim(worker_id="w1")
        assert delivery is not None

        await inbound_broker.dead_letter(delivery, reason="rejected: 400")

        assert await inbound_broker.queue_depth("bot-a") == 0
        assert await inbound_broker.claim(worker_id="w2") is None
