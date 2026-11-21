"""Unit contract of the gram-mq CLI entry point (post feature 006).

The `bot` subcommands are gone: bot lifecycle lives behind the bootstrap
window endpoints. Key dispatch is covered by test_keys_cli.py; here only
the parser shape is asserted.
"""

from __future__ import annotations

import pytest

from gram_mq.cli.main import build_parser, main


def test_parser_has_no_bot_commands() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bot", "add"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bot", "list"])


def test_parser_accepts_key_subcommands() -> None:
    args = build_parser().parse_args(["key", "add"])
    assert (args.command, args.key_command) == ("key", "add")
    args = build_parser().parse_args(["key", "prepare"])
    assert (args.command, args.key_command) == ("key", "prepare")


def test_key_prepare_dispatch_is_the_default_key_fallback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The only non-DB command: runnable on a bare machine (spec 005).
    assert main(["key", "prepare"]) == 0
    assert "Значение активации" in capsys.readouterr().out
