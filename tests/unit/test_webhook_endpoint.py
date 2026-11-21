"""Webhook endpoint unit contract (spec 007: US1, FR-001/002/008/009/012).

Memory inbound broker + fake authenticator: the whole webhook surface
without infrastructure. Codes, artifact freedom on rejects, retry
absorption, and the off-switch when the ports are not injected.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gram_mq.adapters.memory.inbound_broker import (
    InboundMemoryStore,
    InMemoryInboundBroker,
)
from gram_mq.api.app import WebhookCheck, create_app
from gram_mq.ports.broker import Delivery, OutboundMessage

SLUG = "0010-arrakis-melange"
SECRET = "webhook-secret-value"
HEADER = "X-Telegram-Bot-Api-Secret-Token"


class FakeWebhookAuth:
    """Three-state fake: per-bot verdicts driven by the test."""

    def __init__(
        self,
        known_bots: set[str],
        reception_off: frozenset[str] = frozenset(),
        valid_secret: str = SECRET,
    ) -> None:
        self._known = known_bots
        self._off = reception_off
        self._valid = valid_secret

    async def check(self, bot_slug: str, presented: str | None) -> WebhookCheck:
        if bot_slug not in self._known or bot_slug in self._off:
            return "unknown"
        if presented == self._valid:
            return "ok"
        return "rejected"


class FailingBroker:
    """Raises on enqueue: the storage-failure path (503, no artifacts)."""

    async def enqueue_inbound(self, update: object) -> bool:
        raise RuntimeError("storage is down")


@pytest_asyncio.fixture
async def broker() -> InMemoryInboundBroker:
    broker = InMemoryInboundBroker(
        InboundMemoryStore(), now=lambda: datetime.now(UTC), max_attempts=3
    )
    await broker.register_bot(SLUG)
    return broker


def make_client(
    broker: object,
    auth: FakeWebhookAuth,
) -> AsyncClient:
    app = create_app(
        broker=_OutboundStub(),
        key_verifier=_KeyStub(),
        # FakingBroker is not the port type by design (it must only fail).
        inbound_broker=broker,  # type: ignore[arg-type]
        webhook_auth=auth,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _OutboundStub:
    """A no-op BrokerPort: this suite never touches the outbound side."""

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


def update_payload(update_id: int = 1, text: str = "hi") -> dict[str, object]:
    return {"update_id": update_id, "message": {"chat": {"id": 1}, "text": text}}


class TestAuthentication:
    async def test_valid_secret_journaled_200(
        self, broker: InMemoryInboundBroker
    ) -> None:
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            response = await client.post(
                f"/webhooks/{SLUG}", json=update_payload(), headers={HEADER: SECRET}
            )

        assert response.status_code == 200
        assert await broker.queue_depth(SLUG) == 1

    async def test_missing_header_401_no_artifacts(
        self, broker: InMemoryInboundBroker
    ) -> None:
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            response = await client.post(f"/webhooks/{SLUG}", json=update_payload())

        assert response.status_code == 401
        assert await broker.queue_depth(SLUG) == 0

    async def test_wrong_secret_401_no_artifacts(
        self, broker: InMemoryInboundBroker
    ) -> None:
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            response = await client.post(
                f"/webhooks/{SLUG}",
                json=update_payload(),
                headers={HEADER: "forged"},
            )

        assert response.status_code == 401
        assert "forged" not in response.text
        assert await broker.queue_depth(SLUG) == 0

    async def test_unknown_bot_404_no_artifacts(
        self, broker: InMemoryInboundBroker
    ) -> None:
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            response = await client.post(
                "/webhooks/ghost", json=update_payload(), headers={HEADER: SECRET}
            )

        assert response.status_code == 404
        assert await broker.queue_depth(SLUG) == 0

    async def test_reception_off_404_indistinguishable(
        self, broker: InMemoryInboundBroker
    ) -> None:
        # Same code as an unknown bot: no oracle for bot existence.
        async with make_client(
            broker, FakeWebhookAuth({SLUG}, reception_off=frozenset({SLUG}))
        ) as client:
            response = await client.post(
                f"/webhooks/{SLUG}", json=update_payload(), headers={HEADER: SECRET}
            )

        assert response.status_code == 404
        assert await broker.queue_depth(SLUG) == 0


class TestBodyValidation:
    async def test_missing_update_id_422_no_artifacts(
        self, broker: InMemoryInboundBroker
    ) -> None:
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            response = await client.post(
                f"/webhooks/{SLUG}",
                json={"message": {"text": "no id"}},
                headers={HEADER: SECRET},
            )

        assert response.status_code == 422
        assert await broker.queue_depth(SLUG) == 0

    async def test_non_integer_update_id_422(
        self, broker: InMemoryInboundBroker
    ) -> None:
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            response = await client.post(
                f"/webhooks/{SLUG}",
                json={"update_id": "one"},
                headers={HEADER: SECRET},
            )

        assert response.status_code == 422
        assert await broker.queue_depth(SLUG) == 0

    async def test_any_update_type_accepted_verbatim(
        self, broker: InMemoryInboundBroker
    ) -> None:
        # Only update_id is interpreted; callback queries etc. pass through.
        payload = {
            "update_id": 9,
            "callback_query": {"id": "1", "data": "press", "from": {}},
        }
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            response = await client.post(
                f"/webhooks/{SLUG}", json=payload, headers={HEADER: SECRET}
            )

        assert response.status_code == 200
        delivery = await broker.claim(worker_id="w1")
        assert delivery is not None
        assert delivery.payload == payload


class TestDurability:
    async def test_identical_retry_absorbed_200(
        self, broker: InMemoryInboundBroker
    ) -> None:
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            first = await client.post(
                f"/webhooks/{SLUG}", json=update_payload(7), headers={HEADER: SECRET}
            )
            second = await client.post(
                f"/webhooks/{SLUG}", json=update_payload(7), headers={HEADER: SECRET}
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert await broker.queue_depth(SLUG) == 1

    async def test_same_update_id_new_payload_journaled(
        self, broker: InMemoryInboundBroker
    ) -> None:
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            await client.post(
                f"/webhooks/{SLUG}",
                json=update_payload(7, text="old"),
                headers={HEADER: SECRET},
            )
            await client.post(
                f"/webhooks/{SLUG}",
                json=update_payload(7, text="new"),
                headers={HEADER: SECRET},
            )

        assert await broker.queue_depth(SLUG) == 2

    async def test_storage_failure_503_no_artifacts(self) -> None:
        async with make_client(FailingBroker(), FakeWebhookAuth({SLUG})) as client:
            response = await client.post(
                f"/webhooks/{SLUG}", json=update_payload(), headers={HEADER: SECRET}
            )

        assert response.status_code == 503


class TestSurfaceOffWithoutPorts:
    async def test_routes_absent_when_ports_not_injected(
        self, broker: InMemoryInboundBroker
    ) -> None:
        app = create_app(broker=_OutboundStub(), key_verifier=_KeyStub())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/webhooks/{SLUG}", json=update_payload(), headers={HEADER: SECRET}
            )

        assert response.status_code == 404


class TestResponseTimeBudget:
    async def test_success_responses_within_one_second(
        self, broker: InMemoryInboundBroker
    ) -> None:
        # SC-002: with a healthy store every webhook answer must fit in
        # 1 s — the budget behind Telegram's "slow answer = failed
        # delivery". Measures handler overhead (validation + enqueue) on
        # the memory broker; a regression that adds synchronous work to
        # the path fails here before it reaches production retries.
        async with make_client(broker, FakeWebhookAuth({SLUG})) as client:
            for i in range(50):
                started = time.monotonic()
                response = await client.post(
                    f"/webhooks/{SLUG}",
                    json=update_payload(update_id=i + 1),
                    headers={HEADER: SECRET},
                )
                elapsed = time.monotonic() - started
                assert response.status_code == 200
                assert elapsed < 1.0, f"update {i + 1}: {elapsed:.3f}s"
