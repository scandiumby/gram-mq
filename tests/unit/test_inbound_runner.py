"""Inbound worker unit contract (spec 007: US2, FR-003..FR-006).

The runner mapping on the memory inbound broker + fakes, and the pure
``classify`` / ``HttpxUpdateDeliverer`` taxonomy over httpx.MockTransport
— no network, no infrastructure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from httpx import AsyncClient, MockTransport, Response

from gram_mq.adapters.http.deliverer import HttpxUpdateDeliverer, classify
from gram_mq.adapters.memory.inbound_broker import (
    InboundMemoryStore,
    InMemoryInboundBroker,
)
from gram_mq.inbound_worker.runner import BACKOFF_CAP_SECONDS, process_update
from gram_mq.ports.deliverer import DeliveryError
from gram_mq.ports.inbound import InboundUpdate
from tests.conftest import FakeClock


class FakeDeliverer:
    """Scriptable deliverer: outcomes queued by the test."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcomes: list[DeliveryError | None] = []

    async def deliver_update(self, *, update: dict[str, Any]) -> None:
        self.calls.append(update)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


async def enqueue_one(broker: InMemoryInboundBroker, update_id: int = 1) -> Any:
    await broker.enqueue_inbound(
        InboundUpdate(
            id=uuid4(),
            bot_slug="bot-a",
            update_id=update_id,
            payload={"update_id": update_id, "message": {"text": "hi"}},
        )
    )
    delivery = await broker.claim(worker_id="w1")
    assert delivery is not None
    return delivery


def make_broker() -> InMemoryInboundBroker:
    broker = InMemoryInboundBroker(
        InboundMemoryStore(), now=lambda: datetime.now(UTC), max_attempts=3
    )
    return broker


class TestRunnerOutcomes:
    async def test_success_acks_delivered(self) -> None:
        broker = make_broker()
        await broker.register_bot("bot-a")
        delivery = await enqueue_one(broker)

        await process_update(
            delivery,
            broker=broker,
            deliverer=FakeDeliverer(),
            backoff_base=5.0,
            max_attempts=3,
        )

        assert await broker.queue_depth("bot-a") == 0
        row = broker.store.rows[0]
        assert row.status == "delivered"
        assert row.delivered_at is not None

    async def test_permanent_rejection_dead_letters_immediately(self) -> None:
        broker = make_broker()
        await broker.register_bot("bot-a")
        delivery = await enqueue_one(broker)
        deliverer = FakeDeliverer()
        deliverer.outcomes = [DeliveryError("delivery rejected: 404", permanent=True)]

        await process_update(
            delivery,
            broker=broker,
            deliverer=deliverer,
            backoff_base=5.0,
            max_attempts=3,
        )

        row = broker.store.rows[0]
        assert row.status == "failed"
        assert row.error == "delivery rejected: 404"
        assert row.attempts == 0  # no attempts spent on a permanent 4xx

    async def test_rate_limit_pauses_without_spending_attempt(self) -> None:
        broker = make_broker()
        await broker.register_bot("bot-a")
        delivery = await enqueue_one(broker)
        deliverer = FakeDeliverer()
        deliverer.outcomes = [DeliveryError("delivery rate limited", retry_after=7.0)]

        await process_update(
            delivery,
            broker=broker,
            deliverer=deliverer,
            backoff_base=5.0,
            max_attempts=3,
        )

        row = broker.store.rows[0]
        assert row.status == "queued"
        assert row.attempts == 0

    async def test_retryable_error_schedules_exponential_backoff(self) -> None:
        clock = FakeClock()
        broker = InMemoryInboundBroker(
            InboundMemoryStore(), now=clock.now, max_attempts=100
        )
        await broker.register_bot("bot-a")
        deliverer = FakeDeliverer()

        # attempts=0 -> base; attempts=2 -> base*4; attempts>=6 -> cap.
        from datetime import timedelta

        schedule = ((0, 5.0), (2, 20.0), (6, BACKOFF_CAP_SECONDS))
        for index, (attempts, expected_delay) in enumerate(schedule):
            delivery = await enqueue_one(broker, update_id=index + 1)
            delivery.context["attempts"] = attempts  # hint from prior retries
            deliverer.outcomes = [DeliveryError("delivery network error")]
            await process_update(
                delivery,
                broker=broker,
                deliverer=deliverer,
                backoff_base=5.0,
                max_attempts=100,
            )
            row = broker.store.rows[-1]
            assert row.status == "queued"
            assert row.available_at == clock.now() + timedelta(seconds=expected_delay)

    async def test_exhaustion_dead_letters_with_reason(self) -> None:
        broker = make_broker()
        await broker.register_bot("bot-a")
        delivery = await enqueue_one(broker)
        delivery.context["attempts"] = 2  # max_attempts=3: next fails the budget
        deliverer = FakeDeliverer()
        deliverer.outcomes = [DeliveryError("delivery server error: 500")]

        await process_update(
            delivery,
            broker=broker,
            deliverer=deliverer,
            backoff_base=5.0,
            max_attempts=3,
        )

        row = broker.store.rows[0]
        assert row.status == "failed"
        assert row.error == "max attempts exceeded: delivery server error: 500"

    async def test_payload_delivered_verbatim(self) -> None:
        broker = make_broker()
        await broker.register_bot("bot-a")
        delivery = await enqueue_one(broker)
        deliverer = FakeDeliverer()

        await process_update(
            delivery,
            broker=broker,
            deliverer=deliverer,
            backoff_base=5.0,
            max_attempts=3,
        )

        assert deliverer.calls == [delivery.payload]


class TestClassify:
    def test_2xx_success(self) -> None:
        classify(200, None)  # no raise
        classify(204, None)

    def test_429_with_retry_after(self) -> None:
        with pytest.raises(DeliveryError) as exc:
            classify(429, "12")
        assert exc.value.retry_after == 12.0
        assert not exc.value.permanent

    def test_429_without_retry_after_is_retryable(self) -> None:
        with pytest.raises(DeliveryError) as exc:
            classify(429, "Wed, 21 Oct 2026 07:28:00 GMT")
        assert exc.value.retry_after is None
        assert not exc.value.permanent

    def test_5xx_retryable(self) -> None:
        with pytest.raises(DeliveryError) as exc:
            classify(503, None)
        assert not exc.value.permanent

    def test_other_4xx_permanent(self) -> None:
        with pytest.raises(DeliveryError) as exc:
            classify(404, None)
        assert exc.value.permanent

    def test_3xx_permanent(self) -> None:
        with pytest.raises(DeliveryError) as exc:
            classify(302, None)
        assert exc.value.permanent


def mock_deliverer(handler: Any, **kwargs: Any) -> HttpxUpdateDeliverer:
    client = AsyncClient(transport=MockTransport(handler))
    return HttpxUpdateDeliverer(
        url="http://unstable.local/updates", client=client, **kwargs
    )


class TestHttpxDeliverer:
    async def test_posts_payload_with_auth_header(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: Any) -> Response:
            seen["json"] = json.loads(request.content)
            seen["auth"] = request.headers.get("X-API-Key")
            return Response(200)

        deliverer = mock_deliverer(
            handler, auth_header_name="X-API-Key", auth_header_value="bot-side-key"
        )
        payload = {"update_id": 1, "message": {"text": "hi"}}
        await deliverer.deliver_update(update=payload)

        assert seen["json"] == payload
        assert seen["auth"] == "bot-side-key"
        await deliverer.aclose()

    async def test_no_auth_header_when_value_unset(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: Any) -> Response:
            seen["auth"] = request.headers.get("Authorization")
            return Response(200)

        deliverer = mock_deliverer(handler)
        await deliverer.deliver_update(update={"update_id": 1})
        assert seen["auth"] is None
        await deliverer.aclose()

    async def test_transport_error_becomes_retryable(self) -> None:
        def handler(request: Any) -> Response:
            raise httpx.ConnectError("boom")

        deliverer = mock_deliverer(handler)
        with pytest.raises(DeliveryError) as exc:
            await deliverer.deliver_update(update={"update_id": 1})
        assert not exc.value.permanent
        assert "bot-side" not in exc.value.reason
        await deliverer.aclose()

    async def test_429_with_header_maps_retry_after(self) -> None:
        def handler(request: Any) -> Response:
            return Response(429, headers={"Retry-After": "9"})

        deliverer = mock_deliverer(handler)
        with pytest.raises(DeliveryError) as exc:
            await deliverer.deliver_update(update={"update_id": 1})
        assert exc.value.retry_after == 9.0
        await deliverer.aclose()
