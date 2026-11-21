"""Unit contract of the bot management window endpoints (spec 006, US1/US3).

httpx ASGITransport + fake provisioner/revoker: the whole gated surface
without infrastructure. Invariants: 404 while the flag is off, no token
or half material in any response or log, one undifferentiated 503 for
unset/corrupt settings.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from gram_mq.adapters.memory.broker import InMemoryBroker, MemoryStore
from gram_mq.api.app import BotManagement, ProvisionOutcome, create_app

TOKEN = "123456789:" + "a" * 32
OTHER_TOKEN = "987654321:" + "b" * 32


class FakeProvisioner:
    """Callable port double: (name, token) -> outcome."""

    def __init__(
        self,
        fail_with: BaseException | None = None,
        duplicates_after: int | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_with = fail_with
        self.duplicates_after = duplicates_after
        self._created = 0

    async def __call__(self, name: str, token: str) -> ProvisionOutcome:
        self.calls.append((name, token))
        if self.fail_with is not None:
            raise self.fail_with
        self._created += 1
        if self.duplicates_after is not None and self._created > self.duplicates_after:
            return ProvisionOutcome(status="duplicate")
        return ProvisionOutcome(status="created", bot_slug="1234-alpha-beta")


class FakeRevoker:
    def __init__(self, known: set[str]) -> None:
        self.known = known
        self.calls: list[str] = []

    async def __call__(self, bot_slug: str) -> bool:
        self.calls.append(bot_slug)
        return bot_slug in self.known


def make_broker() -> InMemoryBroker:
    return InMemoryBroker(MemoryStore(), max_attempts=3)


def make_client(
    *,
    enabled: bool = True,
    token: str | None = TOKEN,
    provisioner: FakeProvisioner | None = None,
    revoker: FakeRevoker | None = None,
) -> AsyncClient:
    app = create_app(
        broker=make_broker(),
        key_verifier=FakeVerifier(set()),
        bot_management=BotManagement(
            enabled=enabled,
            token=SecretStr(token) if token is not None else None,
        ),
        bot_provisioner=provisioner or FakeProvisioner(),
        bot_revoker=revoker,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class FakeVerifier:
    def __init__(self, valid_keys: set[str]) -> None:
        self.valid_keys = valid_keys

    async def verify(self, key: str | None) -> bool:
        return key in self.valid_keys


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with make_client() as client:
        yield client


class TestCreate:
    async def test_flag_off_is_404(self) -> None:
        async with make_client(enabled=False) as client:
            response = await client.post("/bootstrap/bots", json={"name": "Bot"})
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}

    async def test_happy_path_returns_slug(self, client: AsyncClient) -> None:
        response = await client.post("/bootstrap/bots", json={"name": "My Bot"})
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "created"
        assert body["bot_slug"] == "1234-alpha-beta"

    async def test_response_and_log_carry_no_secrets(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="gram_mq.api"):
            response = await client.post("/bootstrap/bots", json={"name": "My Bot"})
        text = response.text + "".join(record.getMessage() for record in caplog.records)
        assert TOKEN not in text
        assert TOKEN.partition(":")[2][:4] not in text  # no half material

    async def test_invalid_names_are_422(self, client: AsyncClient) -> None:
        for name in ["", "x" * 65, "bad\x00name"]:
            response = await client.post("/bootstrap/bots", json={"name": name})
            assert response.status_code == 422, name

    async def test_missing_token_setting_is_503(self) -> None:
        async with make_client(token=None) as client:
            response = await client.post("/bootstrap/bots", json={"name": "My Bot"})
        assert response.status_code == 503
        assert response.json() == {
            "detail": "bootstrap token or master key missing or invalid"
        }

    async def test_duplicate_live_token_is_409(self) -> None:
        provisioner = FakeProvisioner(duplicates_after=1)
        async with make_client(provisioner=provisioner) as client:
            first = await client.post("/bootstrap/bots", json={"name": "A"})
            second = await client.post("/bootstrap/bots", json={"name": "B"})
        assert first.status_code == 201
        assert second.status_code == 409

    async def test_write_failure_is_500(self) -> None:
        provisioner = FakeProvisioner(fail_with=OSError("disk on fire"))
        async with make_client(provisioner=provisioner) as client:
            response = await client.post("/bootstrap/bots", json={"name": "My Bot"})
        assert response.status_code == 500
        assert TOKEN not in response.text

    async def test_provisioner_receives_name_and_setting_token(
        self, client: AsyncClient
    ) -> None:
        await client.post("/bootstrap/bots", json={"name": "My Bot"})
        # The fake keeps what it was called with; asserting through the
        # fixture would need the handle, so re-check via a fresh pair.
        provisioner = FakeProvisioner()
        async with make_client(provisioner=provisioner) as client2:
            await client2.post("/bootstrap/bots", json={"name": "Second"})
        assert provisioner.calls == [("Second", TOKEN)]


class TestRevoke:
    async def test_flag_off_is_404(self) -> None:
        async with make_client(enabled=False, revoker=FakeRevoker({"x"})) as client:
            response = await client.delete("/bootstrap/bots/x")
        assert response.status_code == 404

    async def test_known_slug_revokes(self) -> None:
        revoker = FakeRevoker({"1234-alpha-beta"})
        async with make_client(revoker=revoker) as client:
            response = await client.delete("/bootstrap/bots/1234-alpha-beta")
        assert response.status_code == 200
        assert response.json() == {
            "status": "revoked",
            "bot_slug": "1234-alpha-beta",
        }
        assert revoker.calls == ["1234-alpha-beta"]

    async def test_unknown_slug_is_404(self) -> None:
        async with make_client(revoker=FakeRevoker(set())) as client:
            response = await client.delete("/bootstrap/bots/0000-x-y")
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}
