"""PostgresInboundBroker: the inbound queue is the journal (spec 007).

Claim is the two-step round-robin of PostgresBroker (feature 001) over
the inbound fairness ledger; expired leases are reclaimed by the same
candidate condition — no janitor job. ``enqueue_inbound`` combines the
registry existence check and the R13 soft-dedup probe in a single SELECT:
an identical row absorbs the retry (False), a differing payload with the
same update_id is a new update (True), a missing bot raises
UnknownBotError.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gram_mq.db import get_session_factory
from gram_mq.models.bots import Bot
from gram_mq.models.inbound import BotInboundClaimState, InboundStatus, InboundUpdateRow
from gram_mq.ports.broker import (
    IllegalDeliveryStateError,
    UnknownBotError,
)
from gram_mq.ports.inbound import (
    InboundBrokerPort,
    InboundDelivery,
    InboundUpdate,
)

Claimable = or_(
    (InboundUpdateRow.status == InboundStatus.queued)
    & (InboundUpdateRow.available_at <= func.now()),
    (InboundUpdateRow.status == InboundStatus.delivering)
    & (InboundUpdateRow.lease_expires_at < func.now()),
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PostgresInboundBroker(InboundBrokerPort):
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
        """Adapter-side seeding helper; not part of InboundBrokerPort."""
        async with self._session() as session:
            await session.execute(
                pg_insert(Bot)
                .values(bot_slug=bot_slug)
                .on_conflict_do_nothing(index_elements=["bot_slug"])
            )
            await session.execute(
                pg_insert(BotInboundClaimState)
                .values(bot_slug=bot_slug)
                .on_conflict_do_nothing(index_elements=["bot_slug"])
            )
            await session.commit()

    async def enqueue_inbound(self, update: InboundUpdate) -> bool:
        async with self._session() as session:
            probe = (
                await session.execute(
                    select(
                        exists()
                        .where(Bot.bot_slug == update.bot_slug)
                        .label("bot_exists"),
                        exists()
                        .where(
                            InboundUpdateRow.bot_slug == update.bot_slug,
                            InboundUpdateRow.update_id == update.update_id,
                            InboundUpdateRow.payload == update.payload,
                        )
                        .label("duplicate"),
                    )
                )
            ).one()
            if not probe.bot_exists:
                raise UnknownBotError(update.bot_slug)
            if probe.duplicate:
                return False  # byte-identical Telegram retry absorbed (R13)
            session.add(
                InboundUpdateRow(
                    id=update.id,
                    bot_slug=update.bot_slug,
                    update_id=update.update_id,
                    payload=update.payload,
                    status=InboundStatus.queued,
                    available_at=_utcnow(),
                    attempts=0,
                    max_attempts=self._max_attempts,
                )
            )
            await session.commit()
            return True

    async def claim(self, *, worker_id: str) -> InboundDelivery | None:
        async with self._session() as session:
            bot = (
                await session.execute(
                    select(BotInboundClaimState.bot_slug)
                    .where(
                        exists()
                        .where(
                            InboundUpdateRow.bot_slug == BotInboundClaimState.bot_slug,
                            Claimable,
                        )
                        .correlate(BotInboundClaimState)
                    )
                    .order_by(BotInboundClaimState.last_claim_at.asc().nulls_first())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if bot is None:
                return None

            candidate = (
                select(InboundUpdateRow.id)
                .where(InboundUpdateRow.bot_slug == bot, Claimable)
                .order_by(InboundUpdateRow.available_at)
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            claimed = (
                (
                    await session.execute(
                        update(InboundUpdateRow)
                        .where(InboundUpdateRow.id == candidate)
                        .values(
                            status=InboundStatus.delivering,
                            locked_at=func.now(),
                            locked_by=worker_id,
                            lease_expires_at=func.now() + self._lease,
                        )
                        .returning(
                            InboundUpdateRow.id,
                            InboundUpdateRow.bot_slug,
                            InboundUpdateRow.update_id,
                            InboundUpdateRow.payload,
                            InboundUpdateRow.attempts,
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
                update(BotInboundClaimState)
                .where(BotInboundClaimState.bot_slug == bot)
                .values(last_claim_at=func.now())
            )
            await session.commit()
            return self._delivery(claimed)

    async def ack(self, delivery: InboundDelivery) -> None:
        result = await self._transition(
            delivery.id,
            statuses=[InboundStatus.delivering],
            values={
                "status": InboundStatus.delivered,
                "delivered_at": func.now(),
            },
        )
        if result != 1:
            raise IllegalDeliveryStateError(f"not delivering: {delivery.id}")

    async def retry(
        self,
        delivery: InboundDelivery,
        delay: timedelta,
        reason: str,
        *,
        count_attempt: bool = True,
    ) -> None:
        values: dict[str, Any] = {
            "status": InboundStatus.queued,
            "available_at": func.now() + delay,
            "error": reason,
        }
        if count_attempt:
            values["attempts"] = InboundUpdateRow.attempts + 1
        result = await self._transition(
            delivery.id,
            statuses=[InboundStatus.delivering],
            values=values,
        )
        if result != 1:
            raise IllegalDeliveryStateError(f"not delivering: {delivery.id}")

    async def dead_letter(self, delivery: InboundDelivery, reason: str) -> None:
        result = await self._transition(
            delivery.id,
            statuses=[InboundStatus.queued, InboundStatus.delivering],
            values={"status": InboundStatus.failed, "error": reason},
        )
        if result != 1:
            raise IllegalDeliveryStateError(f"not claimable: {delivery.id}")

    async def queue_depth(self, bot_slug: str) -> int:
        async with self._session() as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(InboundUpdateRow)
                        .where(
                            InboundUpdateRow.bot_slug == bot_slug,
                            InboundUpdateRow.status.in_(
                                [InboundStatus.queued, InboundStatus.delivering]
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
        update_id: UUID,
        *,
        statuses: list[InboundStatus],
        values: dict[str, Any],
    ) -> int:
        release = {"locked_at": None, "locked_by": None, "lease_expires_at": None}
        async with self._session() as session:
            result = await session.execute(
                update(InboundUpdateRow)
                .where(
                    InboundUpdateRow.id == update_id,
                    InboundUpdateRow.status.in_(statuses),
                )
                .values(**values, **release)
            )
            await session.commit()
            # Result.rowcount exists at runtime; typing stubs disagree.
            return int(getattr(result, "rowcount", 0))

    def _delivery(self, row: Any) -> InboundDelivery:
        return InboundDelivery(
            id=row["id"],
            bot_slug=row["bot_slug"],
            update_id=row["update_id"],
            payload=dict(row["payload"]),
            context={"attempts": row["attempts"]},
        )
