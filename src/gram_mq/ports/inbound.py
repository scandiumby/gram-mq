"""Domain contract of the inbound update queue (spec 007; Constitution I/II).

Sibling of ``BrokerPort`` (feature 001) with identical at-least-once
semantics — lease, attempts, backoff, dead-letter — and inbound-shaped
data: an ``update_id`` instead of a chat id, a verbatim Telegram update
payload, and a ``delivered`` terminal instead of a Telegram message id.
``enqueue_inbound`` is idempotent by (bot_slug, update_id, payload): a
byte-identical Telegram retry is absorbed (False), while the same
update_id with different content is a new update (True) — Telegram
re-picks update_id randomly after a week of bot silence, so a unique
constraint would silently drop real updates (research R13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from gram_mq.ports.broker import IllegalDeliveryStateError, UnknownBotError

__all__ = [
    "IllegalDeliveryStateError",
    "InboundBrokerPort",
    "InboundDelivery",
    "InboundUpdate",
    "UnknownBotError",
]


@dataclass(slots=True)
class InboundUpdate:
    """A Telegram update entering the queue; the producer generates the id."""

    id: UUID
    bot_slug: str
    update_id: int
    payload: dict[str, Any]


@dataclass(slots=True)
class InboundDelivery:
    """The result of claim: an opaque handle owned by the inbound worker.

    ``context`` carries read-only hints (e.g. ``attempts``); the worker
    writes nothing into it — terminal fixation (``delivered_at``) lives in
    the adapter's ``ack``.
    """

    id: UUID
    bot_slug: str
    update_id: int
    payload: dict[str, Any]
    context: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class InboundBrokerPort(Protocol):
    async def enqueue_inbound(self, update: InboundUpdate) -> bool:
        """Record the update with status=queued, available_at=now.

        Returns True when a new row was journaled, False when an identical
        row (same bot_slug + update_id + payload) already exists — a
        Telegram retry absorbed without an artifact; the caller still owes
        Telegram a 200. Raises UnknownBotError when bot_slug is not
        registered.
        """
        ...

    async def claim(self, *, worker_id: str) -> InboundDelivery | None:
        """Take available work into exclusive ownership.

        Candidates: queued rows whose available_at has arrived, plus
        delivering rows whose lease expired (crash reclaim). Fairness
        across bots mirrors the outbound queue. Returns None when there is
        no work.
        """
        ...

    async def ack(self, delivery: InboundDelivery) -> None:
        """Record delivery atomically: status=delivered and delivered_at
        in one transaction.

        Raises IllegalDeliveryStateError when the row is not leased.
        """
        ...

    async def retry(
        self,
        delivery: InboundDelivery,
        delay: timedelta,
        reason: str,
        *,
        count_attempt: bool = True,
    ) -> None:
        """Return the row to queued: available_at=now()+delay, ownership
        released, error=reason. attempts+=1 unless count_attempt=False —
        reserved for rate-limit pauses (429).

        Raises IllegalDeliveryStateError when the row is not leased.
        """
        ...

    async def dead_letter(self, delivery: InboundDelivery, reason: str) -> None:
        """Bury the row: status=failed, error=reason, terminal for claim,
        the row stays in the journal.

        Raises IllegalDeliveryStateError when the row is not claimable.
        """
        ...

    async def queue_depth(self, bot_slug: str) -> int:
        """Number of the bot's undelivered updates (queued + delivering)."""
        ...
