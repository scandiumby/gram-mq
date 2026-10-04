"""Domain contract of the outbound message queue.

Semantics are domain-level, not AMQP (Constitution, Principle I). Delivery
is an opaque adapter handle; the domain never inspects its internals.
New port methods appear only when a concrete adapter needs them (FR-013).
A lease is never renewed in v1: the lease duration must exceed the
worst-case send time; a slow or stuck send resolves through lease expiry
and reclaim (at-least-once, a duplicate is acceptable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


class UnknownBotError(Exception):
    """Enqueue targeted a bot that is not registered (referential integrity)."""


class IllegalDeliveryStateError(Exception):
    """ack/retry/dead_letter hit a row that is not leased by this worker."""


@dataclass(slots=True)
class OutboundMessage:
    """A message entering the queue; the producer generates the id."""

    id: UUID
    bot_slug: str
    chat_id: str
    payload: dict[str, Any]


@dataclass(slots=True)
class Delivery:
    """The result of claim: an opaque handle owned by the worker.

    ``context`` carries read-only hints (e.g. ``attempts``) and, before
    ack, the worker writes ``telegram_message_id`` into it.
    """

    id: UUID
    bot_slug: str
    chat_id: str
    payload: dict[str, Any]
    context: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BrokerPort(Protocol):
    async def enqueue(self, message: OutboundMessage) -> None:
        """Record the message with status=queued, available_at=now.

        Raises UnknownBotError when bot_slug is not registered.
        """
        ...

    async def claim(self, *, worker_id: str) -> Delivery | None:
        """Take available work into exclusive ownership.

        Candidates: queued rows whose available_at has arrived, plus
        leased rows whose lease expired (crash reclaim). Fairness across
        bots: a bot with a huge queue must not starve the others.
        Returns None when there is no work.
        """
        ...

    async def ack(self, delivery: Delivery) -> None:
        """Record delivery atomically: telegram_message_id (from
        delivery.context) and status=sent in one transaction.

        Raises IllegalDeliveryStateError when the row is not leased.
        """
        ...

    async def retry(self, delivery: Delivery, delay: timedelta, reason: str) -> None:
        """Return the row to queued: available_at=now()+delay,
        attempts+=1, ownership released, error=reason.

        Raises IllegalDeliveryStateError when the row is not leased.
        """
        ...

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        """Bury the row: status=failed, error=reason, terminal for claim,
        the row stays in the journal.

        Raises IllegalDeliveryStateError when the row is not claimable.
        """
        ...

    async def queue_depth(self, bot_slug: str) -> int:
        """Number of the bot's undelivered messages (queued + leased)."""
        ...
