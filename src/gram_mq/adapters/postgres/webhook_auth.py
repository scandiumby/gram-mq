"""Webhook authentication over the bots table (spec 007, FR-001/008/009).

One SELECT per request: the bot row carries the HMAC-SHA256 check value
(`webhook_secret_hmac`, written by `gram-mq webhook set`). NULL and a
missing row are the same "unknown" — reception is off or the bot does not
exist, indistinguishable by design (no oracle); the header value is
checked constant-time against the digest. A missing master key cannot
verify anything: "rejected".
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gram_mq.models.bots import Bot
from gram_mq.webhook_store import verify_webhook_secret

WebhookCheck = Literal["ok", "unknown", "rejected"]


class SqlAlchemyWebhookAuth:
    """`check(bot_slug, presented)` — the API layer maps unknown → 404,
    rejected → 401 (contracts/webhook-endpoint.md)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        master_key: bytes | None,
    ) -> None:
        self._session_factory = session_factory
        self._master_key = master_key

    async def check(self, bot_slug: str, presented: str | None) -> WebhookCheck:
        async with self._session_factory() as session:
            row = await session.get(Bot, bot_slug)
        if row is None or not row.webhook_secret_hmac:
            return "unknown"
        if self._master_key is None:
            return "rejected"
        if verify_webhook_secret(self._master_key, row.webhook_secret_hmac, presented):
            return "ok"
        return "rejected"
