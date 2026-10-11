"""Unit contract of token validation and the bots/.env-<slug> file format.

Contract (data-model.md): token is `{numeric_id}:{secret}` with exactly one
colon; the env-file holds GRAMMQ_BOT_TOKEN and GRAMMQ_BOT_NAME; the file is
created exclusively (O_EXCL) with mode 0600; damaged files read as None.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from gram_mq.cli.token import (
    TokenFormatError,
    collect_registered_token_ids,
    read_token_file,
    validate_token_format,
    write_token_file,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123456789:AAFexample-secret", ("123456789", "AAFexample-secret")),
        ("  123456789:AAFexample-secret ", ("123456789", "AAFexample-secret")),
        ("42:x", ("42", "x")),
    ],
)
def test_valid_token_formats(raw: str, expected: tuple[str, str]) -> None:
    assert validate_token_format(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "123456",  # no colon
        "123:abc:def",  # two colons
        ":secret",  # empty id
        "123:",  # empty secret
        "abc:secret",  # non-numeric id
        "",  # empty
        "123:sec\nret",  # embedded newline
        "123:se\x01cret",  # control character in secret
        "12\n3:secret",  # embedded newline in id
    ],
)
def test_invalid_token_formats(raw: str) -> None:
    with pytest.raises(TokenFormatError):
        validate_token_format(raw)


def test_error_message_does_not_leak_the_token() -> None:
    secret = "AAFleak-check-value"
    with pytest.raises(TokenFormatError) as excinfo:
        validate_token_format(f"123:{secret}:tail")  # two colons
    assert secret not in str(excinfo.value)
    with pytest.raises(TokenFormatError) as excinfo:
        validate_token_format(f"123:{secret}\x01")  # control character
    assert secret not in str(excinfo.value)


def test_env_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".env-0010-arrakis-melange"
    write_token_file(path, "10:twenty", "Test = Bot")

    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0] == "GRAMMQ_BOT_TOKEN=10:twenty"
    assert lines[1] == "GRAMMQ_BOT_NAME=Test = Bot"
    assert content.endswith("\n")

    assert read_token_file(path) == ("10", "twenty")


def test_created_file_has_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / ".env-0010-x"
    write_token_file(path, "10:twenty", "N")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_write_refuses_to_overwrite_existing_path(tmp_path: Path) -> None:
    path = tmp_path / ".env-0010-x"
    path.write_text("ORIGINAL", encoding="utf-8")
    with pytest.raises(OSError):
        write_token_file(path, "10:twenty", "N")
    assert path.read_text(encoding="utf-8") == "ORIGINAL"


def test_write_refuses_symlink_path(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("T", encoding="utf-8")
    link = tmp_path / ".env-0010-x"
    link.symlink_to(target)
    with pytest.raises(OSError):
        write_token_file(link, "10:twenty", "N")
    assert target.read_text(encoding="utf-8") == "T"


@pytest.mark.parametrize(
    "content",
    [
        "",  # empty file
        "GRAMMQ_BOT_NAME=Only name\n",  # token key missing
        "GRAMMQ_BOT_TOKEN=not-a-token\n",  # invalid token value
        "GRAMMQ_BOT_TOKEN=\n",  # empty token value
        "garbage without equals\n",
    ],
)
def test_damaged_file_reads_as_none(tmp_path: Path, content: str) -> None:
    path = tmp_path / ".env-0010-x"
    path.write_text(content, encoding="utf-8")
    assert read_token_file(path) is None


def test_collect_registered_token_ids_skips_invalid_files(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env-0010-a").write_text("GRAMMQ_BOT_TOKEN=10:aaa\n", encoding="utf-8")
    (tmp_path / ".env-0020-b").write_text("GRAMMQ_BOT_TOKEN=20:bbb\n", encoding="utf-8")
    (tmp_path / ".env-0030-c").write_text("broken\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("GRAMMQ_BOT_TOKEN=40:ddd\n", encoding="utf-8")

    assert collect_registered_token_ids(tmp_path) == {"10", "20"}
