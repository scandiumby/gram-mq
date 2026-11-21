"""gram-mq CLI entry point: the `key` subcommands (specs 002/005).

The `bot add` wizard was removed with feature 006: bot creation and
revocation live behind the bootstrap window endpoints, and the plaintext
file format the wizard wrote is no longer read by the worker.

Exit codes are the interface contract: 0 ok, 2 preconditions, 4 write
failure (compensated).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from gram_mq.cli.keys import run_key_add, run_key_prepare
from gram_mq.db import get_session_factory
from gram_mq.models.api_keys import ApiKey

EXIT_OK = 0
EXIT_PRECONDITION = 2
EXIT_WRITE = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gram-mq", description="Управление gram-mq")
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("key", help="управление API-ключами")
    key_commands = key.add_subparsers(dest="key_command", required=True)
    key_commands.add_parser("add", help="выдать API-ключ (печатается один раз)")
    key_commands.add_parser(
        "prepare",
        help="сгенерировать ключ и значение активации локально (без БД)",
    )
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


def _key_prepare() -> int:
    # Stage 1 of the shell-less bootstrap (spec 005: FR-002): no
    # Settings, no DB — must run on a bare machine with uv only.
    key, prefix, material = run_key_prepare()
    print("API-ключ создан — показывается один раз:")
    print(key)
    print(f"prefix: {prefix}")
    print()
    print("Значение активации (для GRAMMQ_KEY_BOOTSTRAP_MATERIAL):")
    print(material)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "key" and args.key_command == "add":
        return _key_add()
    return _key_prepare()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
