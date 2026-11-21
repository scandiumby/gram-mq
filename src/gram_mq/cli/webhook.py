"""Webhook administration: `gram-mq webhook set|delete` (spec 007, US3).

The plaintext webhook secret exists only inside `run_webhook_set`, in
CLI memory, for the duration of one `set_webhook` call: the system
generates it, hands it to Telegram, and persists only the HMAC-SHA256
check value (Principle III; FR-008). The Telegram-first ordering is the
safe failure direction: the check value lands in the DB only after
Telegram confirmed it knows the secret — a crash in between leaves
webhooks answering 401 until the command is re-run, never a stored
secret Telegram does not know. Exit codes follow the CLI convention:
0 ok, 2 preconditions, 4 write failure (compensated).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from aiogram import Bot

from gram_mq.webhook_store import make_webhook_secret, webhook_secret_hmac
from gram_mq.worker.bot_config import Ready

#: (bot_slug, check value hex | None) -> persistence; None disables reception.
WebhookStore = Callable[[str, str | None], Awaitable[None]]
#: BotConfig port surface the CLI needs (worker's SplitFileBotConfig).
TokenFor = Callable[[str], Awaitable[object]]


class WebhookPreconditionError(Exception):
    """A precondition failed; nothing has been changed anywhere."""


class TelegramWebhookClient(Protocol):
    async def set_webhook(self, *, token: str, url: str, secret_token: str) -> None: ...

    async def delete_webhook(self, *, token: str) -> None: ...


class AiogramWebhookClient:
    """TelegramWebhookClient over aiogram `Bot`; one short-lived session
    per call — registration is a rare administrative action."""

    async def set_webhook(self, *, token: str, url: str, secret_token: str) -> None:
        bot = Bot(token=token)
        try:
            await bot.set_webhook(url=url, secret_token=secret_token)
        finally:
            await bot.session.close()

    async def delete_webhook(self, *, token: str) -> None:
        bot = Bot(token=token)
        try:
            await bot.delete_webhook()
        finally:
            await bot.session.close()


def build_webhook_url(public_url: str, bot_slug: str) -> str:
    return f"{public_url.rstrip('/')}/webhooks/{bot_slug}"


async def run_webhook_set(
    *,
    bot_slug: str,
    public_url: str | None,
    master_key: bytes | None,
    token_for: TokenFor,
    client: TelegramWebhookClient,
    store: WebhookStore,
) -> str:
    """Register the webhook; returns the public webhook URL.

    Order (contracts/webhook-admin-cli.md): preconditions -> generate
    secret -> Telegram set_webhook -> persist the check value. The
    secret never crosses a return value, a print, or a log line.
    """
    lookup = await token_for(bot_slug)
    if not isinstance(lookup, Ready):
        # NotRegistered (revoked/unknown) or NotReady (unreadable halves,
        # missing master key) — one undifferentiated precondition.
        raise WebhookPreconditionError(
            "bot is not registered or its token halves are unreadable"
        )
    if not public_url:
        raise WebhookPreconditionError("GRAMMQ_WEBHOOK_PUBLIC_URL is not set")
    if master_key is None:
        raise WebhookPreconditionError("GRAMMQ_WEBHOOK_MASTER_KEY is not set")
    url = build_webhook_url(public_url, bot_slug)
    secret = make_webhook_secret()
    await client.set_webhook(token=lookup.token, url=url, secret_token=secret)
    await store(bot_slug, webhook_secret_hmac(master_key, secret))
    return url


async def run_webhook_delete(
    *,
    bot_slug: str,
    token_for: TokenFor,
    client: TelegramWebhookClient,
    store: WebhookStore,
) -> None:
    """Stop reception: Telegram delete_webhook, then clear the check
    value (NULL = reception off -> 404)."""
    lookup = await token_for(bot_slug)
    if not isinstance(lookup, Ready):
        raise WebhookPreconditionError(
            "bot is not registered or its token halves are unreadable"
        )
    await client.delete_webhook(token=lookup.token)
    await store(bot_slug, None)
