"""PostgresBroker: the queue is the journal (Constitution, Stack v1).

Claim is a two-step round-robin (RQ-4) in one transaction: lock the
bot_claim_state row of the bot that went without a claim the longest,
then take its oldest available row FOR UPDATE SKIP LOCKED (RQ-5).
Expired leases are reclaimed by the same candidate condition — no
background janitor job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gram_mq.db import get_session_factory
from gram_mq.models.bots import Bot
from gram_mq.models.claim_state import BotClaimState
from gram_mq.models.messages import Message, MessageStatus
from gram_mq.ports.broker import (
    Delivery,
    IllegalDeliveryStateError,
    OutboundMessage,
    UnknownBotError,
)

Claimable = or_(
    (Message.status == MessageStatus.queued) & (Message.available_at <= func.now()),
    (Message.status == MessageStatus.leased) & (Message.lease_expires_at < func.now()),
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PostgresBroker:
    def __init__(
        self,
        *,
        database_url: str | None = None,
        lease_seconds: float = 60.0,
        max_attempts: int = 5,
    ) -> None:
        self._lease = timedelta(seconds=lease_seconds)
        self._max_attempts = max_attempts
        self._engine: AsyncEngine | None
        self._factory: async_sessionmaker[AsyncSession] | None
        if database_url is not None:
            self._engine = create_async_engine(database_url)
            self._factory = async_sessionmaker(self._engine, expire_on_commit=False)
        else:
            self._engine = None
            self._factory = None

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._factory if self._factory is not None else get_session_factory()

    def _session(self) -> AsyncSession:
        return self._sessions()()

    async def register_bot(self, bot_slug: str) -> None:
        """Adapter-side seeding helper; not part of BrokerPort (FR-013)."""
        async with self._session() as session:
            await session.execute(
                pg_insert(Bot)
                .values(bot_slug=bot_slug)
                .on_conflict_do_nothing(index_elements=["bot_slug"])
            )
            await session.execute(
                pg_insert(BotClaimState)
                .values(bot_slug=bot_slug)
                .on_conflict_do_nothing(index_elements=["bot_slug"])
            )
            await session.commit()

    async def enqueue(self, message: OutboundMessage) -> None:
        async with self._session() as session:
            session.add(
                Message(
                    id=message.id,
                    bot_slug=message.bot_slug,
                    chat_id=message.chat_id,
                    payload=message.payload,
                    status=MessageStatus.queued,
                    available_at=_utcnow(),
                    attempts=0,
                    max_attempts=self._max_attempts,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                raise UnknownBotError(message.bot_slug) from exc

    async def claim(self, *, worker_id: str) -> Delivery | None:
        async with self._session() as session:
            bot = (
                await session.execute(
                    select(BotClaimState.bot_slug)
                    .where(
                        exists()
                        .where(
                            Message.bot_slug == BotClaimState.bot_slug,
                            Claimable,
                        )
                        .correlate(BotClaimState)
                    )
                    .order_by(BotClaimState.last_claim_at.asc().nulls_first())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if bot is None:
                return None

            candidate = (
                select(Message.id)
                .where(Message.bot_slug == bot, Claimable)
                .order_by(Message.available_at)
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            claimed = (
                (
                    await session.execute(
                        update(Message)
                        .where(Message.id == candidate)
                        .values(
                            status=MessageStatus.leased,
                            locked_at=func.now(),
                            locked_by=worker_id,
                            lease_expires_at=func.now() + self._lease,
                        )
                        .returning(
                            Message.id,
                            Message.bot_slug,
                            Message.chat_id,
                            Message.payload,
                            Message.attempts,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if claimed is None:
                await session.rollback()
                return None

            await session.execute(
                update(BotClaimState)
                .where(BotClaimState.bot_slug == bot)
                .values(last_claim_at=func.now())
            )
            await session.commit()
            return self._delivery(claimed)

    async def ack(self, delivery: Delivery) -> None:
        result = await self._transition(
            delivery.id,
            statuses=[MessageStatus.leased],
            values={
                "status": MessageStatus.sent,
                "telegram_message_id": delivery.context.get("telegram_message_id"),
                "sent_at": func.now(),
            },
        )
        if result != 1:
            raise IllegalDeliveryStateError(f"not leased: {delivery.id}")

    async def retry(self, delivery: Delivery, delay: timedelta, reason: str) -> None:
        result = await self._transition(
            delivery.id,
            statuses=[MessageStatus.leased],
            values={
                "status": MessageStatus.queued,
                "available_at": func.now() + delay,
                "attempts": Message.attempts + 1,
                "error": reason,
            },
        )
        if result != 1:
            raise IllegalDeliveryStateError(f"not leased: {delivery.id}")

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        result = await self._transition(
            delivery.id,
            statuses=[MessageStatus.queued, MessageStatus.leased],
            values={"status": MessageStatus.failed, "error": reason},
        )
        if result != 1:
            raise IllegalDeliveryStateError(f"not claimable: {delivery.id}")

    async def queue_depth(self, bot_slug: str) -> int:
        async with self._session() as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Message)
                        .where(
                            Message.bot_slug == bot_slug,
                            Message.status.in_(
                                [MessageStatus.queued, MessageStatus.leased]
                            ),
                        )
                    )
                ).scalar_one()
            )

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    # -- internals ---------------------------------------------------------

    async def _transition(
        self,
        message_id: UUID,
        *,
        statuses: list[MessageStatus],
        values: dict[str, Any],
    ) -> int:
        release = {"locked_at": None, "locked_by": None, "lease_expires_at": None}
        async with self._session() as session:
            result = await session.execute(
                update(Message)
                .where(
                    Message.id == message_id,
                    Message.status.in_(statuses),
                )
                .values(**values, **release)
            )
            await session.commit()
            # Result.rowcount exists at runtime; typing stubs disagree.
            return int(getattr(result, "rowcount", 0))

    def _delivery(self, row: Any) -> Delivery:
        return Delivery(
            id=row["id"],
            bot_slug=row["bot_slug"],
            chat_id=row["chat_id"],
            payload=dict(row["payload"]),
            context={"attempts": row["attempts"]},
        )
