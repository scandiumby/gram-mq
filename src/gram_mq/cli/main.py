"""gram-mq CLI entry point: the `key` subcommands (specs 002/005) and
the `webhook` subcommands (spec 007).

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
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from gram_mq.cli.keys import run_key_add, run_key_prepare
from gram_mq.cli.webhook import (
    AiogramWebhookClient,
    WebhookPreconditionError,
    run_webhook_delete,
    run_webhook_set,
)
from gram_mq.db import get_session_factory
from gram_mq.models.api_keys import ApiKey
from gram_mq.models.bots import Bot

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
    webhook = commands.add_parser("webhook", help="управление вебхуками ботов")
    webhook_commands = webhook.add_subparsers(dest="webhook_command", required=True)
    webhook_commands.add_parser(
        "set", help="зарегистрировать вебхук бота в Telegram"
    ).add_argument("bot_slug")
    webhook_commands.add_parser(
        "delete", help="снять вебхук бота и выключить приём"
    ).add_argument("bot_slug")
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


def _make_webhook_wiring() -> tuple[object, object, bytes | None, str | None]:
    """(token_for, client, webhook master key, public url) from settings."""
    from gram_mq.adapters.postgres.bot_registry import SqlAlchemyBotRegistry
    from gram_mq.settings import get_settings
    from gram_mq.token_store import parse_master_key
    from gram_mq.webhook_store import parse_webhook_master_key
    from gram_mq.worker.bot_config import SplitFileBotConfig

    settings = get_settings()
    registry = SqlAlchemyBotRegistry(get_session_factory())
    bot_config = SplitFileBotConfig(
        bots_dir=Path(settings.bots_dir),
        fetch_half=registry.fetch_half,
        master_key=parse_master_key(settings.token_master_key),
    )
    return (
        bot_config.token_for,
        AiogramWebhookClient(),
        parse_webhook_master_key(settings.webhook_master_key),
        settings.webhook_public_url,
    )


async def _store_webhook_hmac(bot_slug: str, value: str | None) -> None:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            update(Bot)
            .where(Bot.bot_slug == bot_slug)
            .values(webhook_secret_hmac=value)
        )
        await session.commit()


def _webhook_set(bot_slug: str) -> int:
    try:
        token_for, client, master_key, public_url = _make_webhook_wiring()
    except ValidationError:
        print(
            "Ошибка настроек: GRAMMQ_DATABASE_URL обязателен (fail-fast).",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION

    async def body() -> str:
        return await run_webhook_set(
            bot_slug=bot_slug,
            public_url=public_url,
            master_key=master_key,
            token_for=token_for,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            store=_store_webhook_hmac,
        )

    try:
        url = asyncio.run(body())
    except WebhookPreconditionError as error:
        print(f"Предусловие не выполнено: {error}", file=sys.stderr)
        return EXIT_PRECONDITION
    except (OSError, SQLAlchemyError) as error:
        # Telegram already knows the new secret; the check value is not
        # stored — re-run the command. The plaintext secret is gone.
        print(
            f"Сбой записи ({type(error).__name__}); перезапустите команду.",
            file=sys.stderr,
        )
        return EXIT_WRITE
    print(f"Вебхук зарегистрирован: {url}")
    print(
        "Секрет сгенерирован системой и передан Telegram;"
        " хранится только проверочное значение."
    )
    return EXIT_OK


def _webhook_delete(bot_slug: str) -> int:
    try:
        token_for, client, _, _ = _make_webhook_wiring()
    except ValidationError:
        print(
            "Ошибка настроек: GRAMMQ_DATABASE_URL обязателен (fail-fast).",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION

    async def body() -> None:
        await run_webhook_delete(
            bot_slug=bot_slug,
            token_for=token_for,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            store=_store_webhook_hmac,
        )

    try:
        asyncio.run(body())
    except WebhookPreconditionError as error:
        print(f"Предусловие не выполнено: {error}", file=sys.stderr)
        return EXIT_PRECONDITION
    except (OSError, SQLAlchemyError) as error:
        print(
            f"Сбой записи ({type(error).__name__}); перезапустите команду.",
            file=sys.stderr,
        )
        return EXIT_WRITE
    print(f"Вебхук снят, приём обновлений бота {bot_slug} выключен.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "key" and args.key_command == "add":
        return _key_add()
    if args.command == "webhook" and args.webhook_command == "set":
        return _webhook_set(args.bot_slug)
    if args.command == "webhook" and args.webhook_command == "delete":
        return _webhook_delete(args.bot_slug)
    return _key_prepare()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
