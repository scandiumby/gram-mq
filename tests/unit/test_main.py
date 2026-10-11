"""Unit contract of the gram-mq CLI entry point (contracts/cli-bot-add.md).

Exit codes are the interface: 0 ok, 2 preconditions, 3 duplicate, 4
compensated write failure, 130 cancel. The wizard itself is patched; no
database and no real input are touched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import gram_mq.cli.main as main_module
from gram_mq.cli.main import (
    EXIT_CANCEL,
    EXIT_DUPLICATE,
    EXIT_OK,
    EXIT_PRECONDITION,
    EXIT_WRITE,
    build_parser,
    main,
)
from gram_mq.cli.slug import SlugExhaustedError
from gram_mq.cli.wizard import BotAddResult, BotsDirError, DuplicateTokenError

RESULT = BotAddResult(
    bot_slug="0010-arrakis-melange",
    name="Test Bot",
    token_file=Path("/tmp/bots/.env-0010-arrakis-melange"),
)


def test_parser_accepts_bot_add() -> None:
    args = build_parser().parse_args(["bot", "add"])
    assert args.command == "bot"
    assert args.bot_command == "add"


def test_parser_rejects_unknown_bot_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bot", "remove"])


def test_bot_list_is_a_post_mvp_stub(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["bot", "list"]) == EXIT_PRECONDITION
    assert "post-MVP" in capsys.readouterr().out


def _install(
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
    *,
    bots_dir: str = "./bots",
) -> None:
    monkeypatch.setattr(
        main_module, "get_settings", lambda: SimpleNamespace(bots_dir=bots_dir)
    )
    monkeypatch.setattr(main_module, "get_session_factory", lambda: object())

    async def fake_run_bot_add(**_kwargs: object) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(main_module, "run_bot_add", fake_run_bot_add)


def test_successful_registration_prints_summary_without_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, RESULT, bots_dir="/custom/bots")

    assert main(["bot", "add"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "Бот зарегистрирован" in out
    assert "Test Bot" in out
    assert "0010-arrakis-melange" in out
    assert ".env-0010-arrakis-melange" in out


def test_duplicate_token_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, DuplicateTokenError("токен уже зарегистрирован"))
    assert main(["bot", "add"]) == EXIT_DUPLICATE
    assert "токен уже зарегистрирован" in capsys.readouterr().err


def test_bots_dir_and_slug_exhaustion_exit_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, BotsDirError("каталог чужой"))
    assert main(["bot", "add"]) == EXIT_PRECONDITION

    _install(monkeypatch, SlugExhaustedError("нет уникального slug"))
    assert main(["bot", "add"]) == EXIT_PRECONDITION
    assert "нет уникального slug" in capsys.readouterr().err


def test_write_failure_is_compensated_and_exits_four(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, OSError("disk full"))
    assert main(["bot", "add"]) == EXIT_WRITE
    assert "исходном состоянии" in capsys.readouterr().err


def test_interrupt_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, KeyboardInterrupt())
    assert main(["bot", "add"]) == EXIT_CANCEL
    assert "отменена" in capsys.readouterr().err

    _install(monkeypatch, EOFError())
    assert main(["bot", "add"]) == EXIT_CANCEL


def test_missing_database_url_fails_fast(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Hermetic: raise the same ValidationError get_settings() would raise
    # with no GRAMMQ_DATABASE_URL, regardless of a local ./.env file.
    error = ValidationError.from_exception_data(
        "Settings",
        [{"type": "missing", "loc": ("database_url",), "input": {}}],
    )

    def broken_get_settings() -> object:
        raise error

    monkeypatch.setattr(main_module, "get_settings", broken_get_settings)
    assert main(["bot", "add"]) == EXIT_PRECONDITION
    assert "GRAMMQ_DATABASE_URL" in capsys.readouterr().err
