"""The single contract suite for BrokerPort (FR-010).

Runs unchanged against InMemoryBroker (always) and PostgresBroker
(when TEST_DATABASE_URL is set) via the parametrized fixtures in
tests/conftest.py. Scenarios map to user stories 1-5 of
specs/001-broker-port-postgres/spec.md.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

import pytest

from gram_mq.ports.broker import (
    IllegalDeliveryStateError,
    OutboundMessage,
)
from tests.conftest import FakeClock, SeededBroker


def make_message(bot_slug: str = "bot-a", text: str = "hello") -> OutboundMessage:
    return OutboundMessage(
        id=uuid4(), bot_slug=bot_slug, chat_id="42", payload={"text": text}
    )


class TestEnqueue:
    """User Story 1: enqueue and the journal (scenarios 1.1, 1.2)."""

    async def test_enqueue_creates_queued_row_counted_in_depth(
        self, broker: SeededBroker
    ) -> None:
        await broker.register_bot("bot-a")
        await broker.enqueue(make_message())

        assert await broker.queue_depth("bot-a") == 1

    async def test_depth_zero_for_bot_without_messages(
        self, broker: SeededBroker
    ) -> None:
        await broker.register_bot("bot-a")

        assert await broker.queue_depth("bot-a") == 0

    async def test_unknown_bot_rejected(self, broker: SeededBroker) -> None:
        from gram_mq.ports.broker import UnknownBotError

        with pytest.raises(UnknownBotError):
            await broker.enqueue(make_message(bot_slug="ghost"))

    async def test_journal_survives_broker_recreation(
        self, make_broker: Callable[..., SeededBroker]
    ) -> None:
        first = make_broker()
        await first.register_bot("bot-a")
        message = make_message()
        await first.enqueue(message)

        second = make_broker()
        delivery = await second.claim(worker_id="w2")

        assert delivery is not None
        assert delivery.id == message.id

    async def test_enqueue_time_independent_of_journal_depth(
        self, adapter: str, make_broker: Callable[..., SeededBroker]
    ) -> None:
        broker = make_broker()
        await broker.register_bot("bot-a")

        depth = 10_000 if adapter == "memory" else 2_000
        budget = 0.05 if adapter == "memory" else 2.0

        for _ in range(depth):
            await broker.enqueue(make_message())

        start = time.monotonic()
        await broker.enqueue(make_message())
        single = time.monotonic() - start

        assert single < budget, single


class TestClaim:
    """User Story 2: exclusive ownership and fairness (scenarios 2.1-2.3)."""

    async def test_claim_leases_row_with_owner_and_deadline(
        self, broker: SeededBroker
    ) -> None:
        await broker.register_bot("bot-a")
        await broker.enqueue(make_message())

        delivery = await broker.claim(worker_id="w1")

        assert delivery is not None
        assert delivery.bot_slug == "bot-a"
        assert delivery.payload["text"] == "hello"
        assert delivery.context["attempts"] == 0
        # The row is now leased: nothing else is claimable (scenario 2.2).
        assert await broker.claim(worker_id="w2") is None

    async def test_empty_queue_returns_none(self, broker: SeededBroker) -> None:
        await broker.register_bot("bot-a")

        assert await broker.claim(worker_id="w1") is None

    async def test_fairness_no_starvation(
        self,
        adapter: str,
        make_broker: Callable[..., SeededBroker],
    ) -> None:
        broker = make_broker()
        for slug in ("bot-a", "bot-b", "bot-c"):
            await broker.register_bot(slug)

        bulk = 1000 if adapter == "memory" else 200
        for _ in range(bulk):
            await broker.enqueue(make_message(bot_slug="bot-a"))
        await broker.enqueue(make_message(bot_slug="bot-b"))
        await broker.enqueue(make_message(bot_slug="bot-c"))

        claimed_bots = []
        for _ in range(10):
            delivery = await broker.claim(worker_id="w1")
            if delivery is not None:
                claimed_bots.append(delivery.bot_slug)

        assert len(set(claimed_bots)) >= 2


class TestLeaseExpiry:
    """User Story 3: crash reclaim of expired leases (SC-001)."""

    async def test_expired_lease_reclaimed_by_other_worker(
        self,
        adapter: str,
        make_broker: Callable[..., SeededBroker],
        clock: FakeClock,
    ) -> None:
        broker = make_broker(lease_seconds=0.2)
        await broker.register_bot("bot-a")
        message = make_message()
        await broker.enqueue(message)

        first = await broker.claim(worker_id="w1")
        assert first is not None

        if adapter == "memory":
            clock.advance(timedelta(seconds=0.3))
        else:
            await asyncio.sleep(0.3)

        second = await broker.claim(worker_id="w2")
        assert second is not None
        assert second.id == message.id

    async def test_live_lease_not_reclaimed(
        self,
        adapter: str,
        make_broker: Callable[..., SeededBroker],
        clock: FakeClock,
    ) -> None:
        broker = make_broker(lease_seconds=60.0)
        await broker.register_bot("bot-a")
        await broker.enqueue(make_message())

        assert await broker.claim(worker_id="w1") is not None

        if adapter == "memory":
            clock.advance(timedelta(seconds=1))
        else:
            await asyncio.sleep(0.05)

        assert await broker.claim(worker_id="w2") is None


class TestOutcome:
    """User Story 4: ack / retry / dead_letter (scenarios 4.1-4.3)."""

    async def test_ack_records_sent_atomically(self, broker: SeededBroker) -> None:
        await broker.register_bot("bot-a")
        await broker.enqueue(make_message())
        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None

        delivery.context["telegram_message_id"] = "777"
        await broker.ack(delivery)

        assert await broker.queue_depth("bot-a") == 0
        with pytest.raises(IllegalDeliveryStateError):
            await broker.ack(delivery)

    async def test_retry_enforces_backoff_then_returns(
        self,
        adapter: str,
        make_broker: Callable[..., SeededBroker],
        clock: FakeClock,
    ) -> None:
        broker = make_broker()
        await broker.register_bot("bot-a")
        await broker.enqueue(make_message())
        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None

        delay = timedelta(seconds=30) if adapter == "memory" else timedelta(seconds=0.3)
        await broker.retry(delivery, delay=delay, reason="rate_limited")

        if adapter == "memory":
            assert await broker.claim(worker_id="w2") is None
            clock.advance(delay + timedelta(seconds=1))
        else:
            await asyncio.sleep(0.05)
            assert await broker.claim(worker_id="w2") is None
            await asyncio.sleep(0.4)

        retried = await broker.claim(worker_id="w2")
        assert retried is not None
        assert retried.context["attempts"] == 1

    async def test_retry_requires_leased_row(self, broker: SeededBroker) -> None:
        await broker.register_bot("bot-a")
        await broker.enqueue(make_message())
        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None
        delivery.context["telegram_message_id"] = "1"
        await broker.ack(delivery)

        with pytest.raises(IllegalDeliveryStateError):
            await broker.retry(delivery, delay=timedelta(seconds=1), reason="x")

    async def test_dead_letter_is_terminal(self, broker: SeededBroker) -> None:
        await broker.register_bot("bot-a")
        await broker.enqueue(make_message())
        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None

        await broker.dead_letter(delivery, reason="telegram_error:blocked")

        assert await broker.claim(worker_id="w2") is None
        assert await broker.queue_depth("bot-a") == 0


class TestQueueDepth:
    """User Story 5: depth semantics across transitions (FR-009)."""

    async def test_depth_counts_queued_and_leased_not_terminal(
        self, broker: SeededBroker
    ) -> None:
        await broker.register_bot("bot-a")
        await broker.enqueue(make_message())
        await broker.enqueue(make_message())

        assert await broker.queue_depth("bot-a") == 2

        first = await broker.claim(worker_id="w1")
        assert first is not None
        assert await broker.queue_depth("bot-a") == 2  # leased is undelivered

        first.context["telegram_message_id"] = "1"
        await broker.ack(first)
        assert await broker.queue_depth("bot-a") == 1

        second = await broker.claim(worker_id="w1")
        assert second is not None
        await broker.dead_letter(second, reason="boom")
        assert await broker.queue_depth("bot-a") == 0

    async def test_depths_isolated_per_bot(self, broker: SeededBroker) -> None:
        await broker.register_bot("bot-a")
        await broker.register_bot("bot-b")

        await broker.enqueue(make_message(bot_slug="bot-a"))
        await broker.enqueue(make_message(bot_slug="bot-a"))
        await broker.enqueue(make_message(bot_slug="bot-b"))

        assert await broker.queue_depth("bot-a") == 2
        assert await broker.queue_depth("bot-b") == 1
        assert await broker.queue_depth("bot-c") == 0
