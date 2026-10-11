"""Unit contract of the BotConfig file adapter (spec 003, FR-008)."""

from __future__ import annotations

from pathlib import Path

from gram_mq.worker.bot_config import FileBotConfig


async def test_valid_file_yields_token(tmp_path: Path) -> None:
    (tmp_path / ".env-0010-a-b").write_text(
        "GRAMMQ_BOT_TOKEN=10:twenty\nGRAMMQ_BOT_NAME=Test\n",
        encoding="utf-8",
    )
    config = FileBotConfig(tmp_path)
    assert await config.token_for("0010-a-b") == "10:twenty"


async def test_missing_file_yields_none(tmp_path: Path) -> None:
    config = FileBotConfig(tmp_path)
    assert await config.token_for("0010-nope") is None


async def test_damaged_file_yields_none(tmp_path: Path) -> None:
    (tmp_path / ".env-0010-bad").write_text("garbage\n", encoding="utf-8")
    config = FileBotConfig(tmp_path)
    assert await config.token_for("0010-bad") is None
