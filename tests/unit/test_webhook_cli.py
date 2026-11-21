"""Webhook CLI unit contract (spec 007: US3, FR-007/008).

Fakes for the Telegram client and the store; the pure command bodies
carry the load-bearing logic — the Telegram-first ordering, the
precondition gate, and the never-printed plaintext secret.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from gram_mq.cli.main import build_parser
from gram_mq.cli.webhook import (
    WebhookPreconditionError,
    build_webhook_url,
    run_webhook_delete,
    run_webhook_set,
)
from gram_mq.worker.bot_config import NotReady, Ready

SLUG = "0010-arrakis-melange"
TOKEN = "123456:abcdef"
PUBLIC = "https://gram.example.com"
KEY = bytes(range(32))  # any 32 bytes; HMAC does not care
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def set_webhook(self, *, token: str, url: str, secret_token: str) -> None:
        self.calls.append(("set", {"token": token, "url": url, "secret": secret_token}))

    async def delete_webhook(self, *, token: str) -> None:
        self.calls.append(("delete", {"token": token}))


class RecordingStore:
    def __init__(self, fail: bool = False) -> None:
        self.values: list[tuple[str, str | None]] = []
        self.fail = fail

    async def __call__(self, bot_slug: str, value: str | None) -> None:
        if self.fail:
            raise OSError("db write failed")
        self.values.append((bot_slug, value))


def ready() -> Any:
    class _TokenFor:
        async def __call__(self, bot_slug: str) -> Any:
            return Ready(TOKEN)

    return _TokenFor()


def not_ready() -> Any:
    class _TokenFor:
        async def __call__(self, bot_slug: str) -> Any:
            return NotReady()

    return _TokenFor()


class TestWebhookSet:
    async def test_telegram_first_then_store_hmac(self) -> None:
        client = RecordingClient()
        store = RecordingStore()

        url = await run_webhook_set(
            bot_slug=SLUG,
            public_url=PUBLIC,
            master_key=KEY,
            token_for=ready(),
            client=client,
            store=store,
        )

        assert url == f"{PUBLIC}/webhooks/{SLUG}"
        assert len(client.calls) == 1
        kind, args = client.calls[0]
        assert kind == "set"
        assert args["token"] == TOKEN
        assert args["url"] == url
        assert len(store.values) == 1
        slug, stored = store.values[0]
        assert slug == SLUG
        # The stored value is the hex digest, never the plaintext secret.
        assert HEX64.match(stored or "")
        assert stored != args["secret"]

    async def test_unreadable_bot_is_precondition_without_side_effects(self) -> None:
        client = RecordingClient()
        store = RecordingStore()

        with pytest.raises(WebhookPreconditionError):
            await run_webhook_set(
                bot_slug=SLUG,
                public_url=PUBLIC,
                master_key=KEY,
                token_for=not_ready(),
                client=client,
                store=store,
            )

        assert client.calls == []
        assert store.values == []

    async def test_missing_public_url_is_precondition(self) -> None:
        with pytest.raises(WebhookPreconditionError):
            await run_webhook_set(
                bot_slug=SLUG,
                public_url=None,
                master_key=KEY,
                token_for=ready(),
                client=RecordingClient(),
                store=RecordingStore(),
            )

    async def test_missing_master_key_is_precondition(self) -> None:
        with pytest.raises(WebhookPreconditionError):
            await run_webhook_set(
                bot_slug=SLUG,
                public_url=PUBLIC,
                master_key=None,
                token_for=ready(),
                client=RecordingClient(),
                store=RecordingStore(),
            )

    async def test_store_failure_propagates_after_telegram(self) -> None:
        # Exit code 4 at the wiring level: Telegram knows the new secret,
        # the check value is not stored — re-run is the recovery.
        with pytest.raises(OSError):
            await run_webhook_set(
                bot_slug=SLUG,
                public_url=PUBLIC,
                master_key=KEY,
                token_for=ready(),
                client=RecordingClient(),
                store=RecordingStore(fail=True),
            )

    async def test_re_run_overwrites_with_a_fresh_check_value(self) -> None:
        store = RecordingStore()

        await run_webhook_set(
            bot_slug=SLUG,
            public_url=PUBLIC,
            master_key=KEY,
            token_for=ready(),
            client=RecordingClient(),
            store=store,
        )
        await run_webhook_set(
            bot_slug=SLUG,
            public_url=PUBLIC,
            master_key=KEY,
            token_for=ready(),
            client=RecordingClient(),
            store=store,
        )

        first, second = store.values
        assert first[1] != second[1]  # a fresh secret each run


class TestWebhookDelete:
    async def test_telegram_first_then_null_check_value(self) -> None:
        client = RecordingClient()
        store = RecordingStore()

        await run_webhook_delete(
            bot_slug=SLUG,
            token_for=ready(),
            client=client,
            store=store,
        )

        assert client.calls == [("delete", {"token": TOKEN})]
        assert store.values == [(SLUG, None)]  # NULL = reception off

    async def test_unreadable_bot_is_precondition(self) -> None:
        with pytest.raises(WebhookPreconditionError):
            await run_webhook_delete(
                bot_slug=SLUG,
                token_for=not_ready(),
                client=RecordingClient(),
                store=RecordingStore(),
            )


class TestUrlBuilding:
    def test_trailing_slash_normalized(self) -> None:
        assert (
            build_webhook_url("https://gram.example.com/", SLUG)
            == f"https://gram.example.com/webhooks/{SLUG}"
        )


class TestParser:
    def test_webhook_set_slug_parsed(self) -> None:
        args = build_parser().parse_args(["webhook", "set", SLUG])
        assert args.command == "webhook"
        assert args.webhook_command == "set"
        assert args.bot_slug == SLUG

    def test_webhook_delete_slug_parsed(self) -> None:
        args = build_parser().parse_args(["webhook", "delete", SLUG])
        assert args.webhook_command == "delete"
        assert args.bot_slug == SLUG
