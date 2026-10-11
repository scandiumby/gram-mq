"""gram-mq CLI entry point (spec 002): the `gram-mq bot add` wizard.

Exit codes are the interface contract (contracts/cli-bot-add.md):
0 registered, 2 preconditions, 3 duplicate token, 4 write failure
(compensated), 130 cancelled by the operator.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from gram_mq.cli.keys import run_key_add
from gram_mq.cli.slug import SlugExhaustedError
from gram_mq.cli.wizard import (
    BotsDirError,
    DuplicateTokenError,
    SqlAlchemyBotRegistry,
    run_bot_add,
)
from gram_mq.db import get_session_factory
from gram_mq.models.api_keys import ApiKey
from gram_mq.settings import get_settings

EXIT_OK = 0
EXIT_PRECONDITION = 2
EXIT_DUPLICATE = 3
EXIT_WRITE = 4
EXIT_CANCEL = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gram-mq", description="Управление gram-mq")
    commands = parser.add_subparsers(dest="command", required=True)
    bot = commands.add_parser("bot", help="управление ботами")
    bot_commands = bot.add_subparsers(dest="bot_command", required=True)
    bot_commands.add_parser("add", help="зарегистрировать бота (визард)")
    bot_commands.add_parser("list", help="список зарегистрированных ботов")
    key = commands.add_parser("key", help="управление API-ключами")
    key_commands = key.add_subparsers(dest="key_command", required=True)
    key_commands.add_parser("add", help="выдать API-ключ (печатается один раз)")
    return parser


def _key_add() -> int:
    async def insert(prefix: str, key_hash: str) -> None:
        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                session.add(ApiKey(prefix=prefix, key_hash=key_hash))

    try:
        key, prefix = asyncio.run(run_key_add(insert=insert))
    except ValidationError:
        print(
            "Ошибка настроек: GRAMMQ_DATABASE_URL обязателен (fail-fast).",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION
    except (OSError, SQLAlchemyError) as error:
        print(
            f"Сбой записи ({type(error).__name__}); ключ не выдан.",
            file=sys.stderr,
        )
        return EXIT_WRITE
    print("API-ключ создан — показывается один раз:")
    print(key)
    print(f"prefix: {prefix}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "bot" and args.bot_command == "list":
        print("Команда 'bot list' появится после US2 (post-MVP).")
        return EXIT_PRECONDITION
    if args.command == "key" and args.key_command == "add":
        return _key_add()

    try:
        settings = get_settings()
    except ValidationError:
        print(
            "Ошибка настроек: GRAMMQ_DATABASE_URL обязателен (fail-fast).",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION

    try:
        result = asyncio.run(
            run_bot_add(
                bots_dir=Path(settings.bots_dir),
                registry=SqlAlchemyBotRegistry(get_session_factory()),
                input_fn=input,
                token_input_fn=getpass.getpass,
            )
        )
    except DuplicateTokenError as error:
        print(f"Отказ: {error}", file=sys.stderr)
        return EXIT_DUPLICATE
    except (BotsDirError, SlugExhaustedError) as error:
        print(f"Отказ: {error}", file=sys.stderr)
        return EXIT_PRECONDITION
    except (OSError, SQLAlchemyError) as error:
        print(
            f"Сбой записи ({type(error).__name__}); система в исходном состоянии.",
            file=sys.stderr,
        )
        return EXIT_WRITE
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C and Ctrl+D are the same operator cancel (FR-009: no
        # artifacts; the wizard compensates anything already created).
        print("Регистрация отменена.", file=sys.stderr)
        return EXIT_CANCEL

    print("Бот зарегистрирован")
    print(f"  имя:  {result.name}")
    print(f"  slug: {result.bot_slug}")
    print(f"  файл: {result.token_file.resolve()}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
