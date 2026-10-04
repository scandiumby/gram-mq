"""In-memory BrokerPort double for tests (Constitution, Testing).

Same semantics as PostgresBroker: statuses, leases, attempts, fairness.
The store is injectable so several broker instances (a "restart") share
one journal. Time comes from an injectable clock for deterministic
lease-expiry tests (RQ-8).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from gram_mq.ports.broker import (
    BrokerPort,
    Delivery,
    IllegalDeliveryStateError,
    OutboundMessage,
    UnknownBotError,
)

QUEUED = "queued"
LEASED = "leased"
SENT = "sent"
FAILED = "failed"
UNDELIVERED = (QUEUED, LEASED)


def _system_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Row:
    id: UUID
    bot_slug: str
    chat_id: str
    payload: dict[str, Any]
    status: str = QUEUED
    available_at: datetime | None = None
    locked_at: datetime | None = None
    locked_by: str | None = None
    lease_expires_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = 5
    telegram_message_id: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    sent_at: datetime | None = None


@dataclass
class MemoryStore:
    """Shared journal: rows, registered bots, and fairness state."""

    rows: list[_Row] = field(default_factory=list)
    bots: set[str] = field(default_factory=set)
    last_claim: dict[str, datetime | None] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InMemoryBroker(BrokerPort):
    def __init__(
        self,
        store: MemoryStore | None = None,
        *,
        now: Callable[[], datetime] = _system_now,
        lease_seconds: float = 60.0,
        max_attempts: int = 5,
    ) -> None:
        self._store = store if store is not None else MemoryStore()
        self._now = now
        self._lease = timedelta(seconds=lease_seconds)
        self._max_attempts = max_attempts

    async def register_bot(self, bot_slug: str) -> None:
        """Adapter-side seeding helper; not part of BrokerPort (FR-013)."""
        self._store.bots.add(bot_slug)
        self._store.last_claim.setdefault(bot_slug, None)

    async def enqueue(self, message: OutboundMessage) -> None:
        if message.bot_slug not in self._store.bots:
            raise UnknownBotError(message.bot_slug)
        moment = self._now()
        self._store.rows.append(
            _Row(
                id=message.id,
                bot_slug=message.bot_slug,
                chat_id=message.chat_id,
                payload=dict(message.payload),
                available_at=moment,
                created_at=moment,
                max_attempts=self._max_attempts,
            )
        )

    async def claim(self, *, worker_id: str) -> Delivery | None:
        async with self._store.lock:
            moment = self._now()
            row = self._pick_claimable(moment)
            if row is None:
                return None
            row.status = LEASED
            row.locked_at = moment
            row.locked_by = worker_id
            row.lease_expires_at = moment + self._lease
            self._store.last_claim[row.bot_slug] = moment
            return self._delivery(row)

    async def ack(self, delivery: Delivery) -> None:
        row = self._by_id(delivery.id)
        if row is None or row.status != LEASED:
            raise IllegalDeliveryStateError(f"not leased: {delivery.id}")
        moment = self._now()
        row.status = SENT
        row.telegram_message_id = delivery.context.get("telegram_message_id")
        row.sent_at = moment
        self._release(row)

    async def retry(self, delivery: Delivery, delay: timedelta, reason: str) -> None:
        row = self._by_id(delivery.id)
        if row is None or row.status != LEASED:
            raise IllegalDeliveryStateError(f"not leased: {delivery.id}")
        row.status = QUEUED
        row.available_at = self._now() + delay
        row.attempts += 1
        row.error = reason
        self._release(row)

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        row = self._by_id(delivery.id)
        if row is None or row.status not in (QUEUED, LEASED):
            raise IllegalDeliveryStateError(f"not claimable: {delivery.id}")
        row.status = FAILED
        row.error = reason
        self._release(row)

    async def queue_depth(self, bot_slug: str) -> int:
        return sum(
            1
            for row in self._store.rows
            if row.bot_slug == bot_slug and row.status in UNDELIVERED
        )

    async def aclose(self) -> None:
        return None

    # -- internals ---------------------------------------------------------

    def _claimable(self, row: _Row, moment: datetime) -> bool:
        if row.status == QUEUED:
            return row.available_at is not None and row.available_at <= moment
        if row.status == LEASED:
            return row.lease_expires_at is not None and row.lease_expires_at < moment
        return False

    def _pick_claimable(self, moment: datetime) -> _Row | None:
        """Fairness (RQ-4): among bots with claimable rows, serve the one
        that went without a claim the longest (never claimed first)."""
        claimable_by_bot: dict[str, list[_Row]] = {}
        for row in self._store.rows:
            if self._claimable(row, moment):
                claimable_by_bot.setdefault(row.bot_slug, []).append(row)
        if not claimable_by_bot:
            return None
        bot = min(
            claimable_by_bot,
            key=lambda slug: (
                self._store.last_claim.get(slug) or datetime.min.replace(tzinfo=UTC)
            ),
        )
        rows = claimable_by_bot[bot]
        return min(rows, key=lambda row: row.available_at or moment)

    def _by_id(self, message_id: UUID) -> _Row | None:
        for row in self._store.rows:
            if row.id == message_id:
                return row
        return None

    def _release(self, row: _Row) -> None:
        row.locked_at = None
        row.locked_by = None
        row.lease_expires_at = None

    def _delivery(self, row: _Row) -> Delivery:
        return Delivery(
            id=row.id,
            bot_slug=row.bot_slug,
            chat_id=row.chat_id,
            payload=dict(row.payload),
            context={"attempts": row.attempts},
        )
