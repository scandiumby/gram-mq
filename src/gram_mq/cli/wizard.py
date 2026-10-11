"""Interactive `gram-mq bot add` wizard (spec 002, US1: FR-001..FR-014).

Atomicity (FR-009): the token file is created first, the registry row
second; any failure, cancel or interrupt after file creation compensates
with an unlink, leaving the system in its initial state. The token secret
lives only in the file (FR-008) and never reaches prompts or errors.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gram_mq.cli.slug import SlugExhaustedError, SlugRng, generate_bot_slug
from gram_mq.cli.token import (
    TokenFormatError,
    collect_registered_token_ids,
    validate_token_format,
    write_token_file,
)
from gram_mq.models.bots import Bot
from gram_mq.models.claim_state import BotClaimState

MAX_NAME_LENGTH = 64
REGISTRATION_ATTEMPTS = 50


class DuplicateTokenError(RuntimeError):
    """The token id is already registered (FR-012)."""


class BotsDirError(RuntimeError):
    """bots/ is unusable: wrong path type or another user's directory (FR-014)."""


@dataclass(slots=True)
class BotAddResult:
    bot_slug: str
    name: str
    token_file: Path


class BotRegistry(Protocol):
    """The registry surface the wizard needs (the bots table)."""

    async def contains(self, bot_slug: str) -> bool: ...

    async def add(self, bot_slug: str) -> None: ...


class SqlAlchemyBotRegistry:
    """BotRegistry over the bots table via an async session factory."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def contains(self, bot_slug: str) -> bool:
        async with self._session_factory() as session:
            return await session.get(Bot, bot_slug) is not None

    async def add(self, bot_slug: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                session.add(Bot(bot_slug=bot_slug))
                # The claim fairness ledger must know every registered
                # bot: PostgresBroker.claim iterates bot_claim_state, and
                # a bot without a row there is invisible to the worker
                # (001 contract; register_bot seeds both tables).
                await session.execute(
                    pg_insert(BotClaimState)
                    .values(bot_slug=bot_slug)
                    .on_conflict_do_nothing(index_elements=["bot_slug"])
                )


def _validate_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise ValueError("имя бота не может быть пустым")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"имя бота длиннее {MAX_NAME_LENGTH} символов")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("имя бота содержит управляющие символы")
    return name


def _check_bots_dir(bots_dir: Path) -> None:
    if bots_dir.exists():
        if not bots_dir.is_dir():
            raise BotsDirError(f"{bots_dir} не каталог")
        if bots_dir.stat().st_uid != os.geteuid():
            raise BotsDirError(f"каталог {bots_dir} принадлежит другому пользователю")
    else:
        bots_dir.mkdir(mode=0o700)
        os.chmod(bots_dir, 0o700)


def _ask_name(input_fn: Callable[[str], str]) -> str:
    while True:
        try:
            return _validate_name(input_fn("Имя бота: "))
        except ValueError as error:
            print(f"Ошибка: {error}")


def _ask_token(token_input_fn: Callable[[str], str]) -> tuple[str, str]:
    while True:
        try:
            return validate_token_format(token_input_fn("Токен бота ({id}:{secret}): "))
        except TokenFormatError as error:
            print(f"Ошибка: {error}")


async def run_bot_add(
    *,
    bots_dir: Path,
    registry: BotRegistry,
    input_fn: Callable[[str], str],
    token_input_fn: Callable[[str], str],
    rng: SlugRng | None = None,
) -> BotAddResult:
    """Run the wizard; see contracts/cli-bot-add.md for the UX contract."""
    _check_bots_dir(bots_dir)
    name = _ask_name(input_fn)
    token_id, secret = _ask_token(token_input_fn)

    if token_id in collect_registered_token_ids(bots_dir):
        raise DuplicateTokenError("токен с таким идентификатором уже зарегистрирован")

    chooser = rng or random.SystemRandom()
    for _ in range(REGISTRATION_ATTEMPTS):
        slug = generate_bot_slug(
            token_id,
            is_taken=lambda candidate: os.path.lexists(bots_dir / f".env-{candidate}"),
            rng=chooser,
        )
        if await registry.contains(slug):
            continue
        token_file = bots_dir / f".env-{slug}"
        try:
            write_token_file(token_file, f"{token_id}:{secret}", name)
            await registry.add(slug)
        except IntegrityError:
            # Lost the uniqueness race; the exclusive lock is post-MVP
            # (FR-013 in Future Enhancements) — compensate and regenerate.
            token_file.unlink(missing_ok=True)
            continue
        except BaseException:
            token_file.unlink(missing_ok=True)
            raise
        return BotAddResult(bot_slug=slug, name=name, token_file=token_file)

    raise SlugExhaustedError(
        f"не удалось подобрать уникальный slug за {REGISTRATION_ATTEMPTS} попыток"
    )
