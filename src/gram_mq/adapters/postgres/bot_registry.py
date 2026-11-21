"""Bots-table registry adapter (spec 006; wizard heritage, spec 002).

Everything the bot admin flow and the worker need from the registry:
existence, encrypted-half storage (with the claim-state seeding contract
of feature 001 — a bot invisible to `bot_claim_state` is invisible to
the worker), the half lookup for delivery, and the hard delete of a
revoked bot.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gram_mq.models.bots import Bot
from gram_mq.models.claim_state import BotClaimState


class SqlAlchemyBotRegistry:
    """Registry surface over `gmq_bots` via an async session factory."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def contains(self, bot_slug: str) -> bool:
        async with self._session_factory() as session:
            return await session.get(Bot, bot_slug) is not None

    async def add(self, bot_slug: str, *, token_half: str, pair_nonce: str) -> None:
        """Insert the bot row and seed the claim fairness ledger in one
        transaction; the PK settles concurrent provisioning races."""
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    Bot(
                        bot_slug=bot_slug,
                        token_half=token_half,
                        pair_nonce=pair_nonce,
                    )
                )
                await session.execute(
                    pg_insert(BotClaimState)
                    .values(bot_slug=bot_slug)
                    .on_conflict_do_nothing(index_elements=["bot_slug"])
                )

    async def fetch_half(self, bot_slug: str) -> tuple[str, str] | None:
        """(token_half, pair_nonce) of the row; None only when the row
        itself is absent. A pre-feature row with NULL halves returns
        ("", "") — the bot exists (registered), its halves are unreadable
        (NotReady), and the worker must not mistake it for a revoked one.
        """
        async with self._session_factory() as session:
            row = await session.get(Bot, bot_slug)
            if row is None:
                return None
            return row.token_half or "", row.pair_nonce or ""

    async def delete(self, bot_slug: str) -> bool:
        """Hard revoke of the registry row. The claim-state ledger row
        deliberately survives (the claim round-robin iterates it): it is
        what keeps a revoked bot's queued messages claimable until the
        worker dead-letters them as NotRegistered. False when the slug
        was already gone."""
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    delete(Bot).where(Bot.bot_slug == bot_slug)
                )
                return bool(cast(CursorResult[Any], result).rowcount)
