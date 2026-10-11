"""Unit contract of the `gram-mq bot add` wizard (spec 002: US1, FR-001..FR-014).

The registry, inputs and rng are injected; the bots/ directory is a tmp
path. Synthetic secrets only (`10:test-secret`), never real ones.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import gram_mq.cli.slug as slug_module
from gram_mq.cli.wizard import (
    BotAddResult,
    BotRegistry,
    BotsDirError,
    DuplicateTokenError,
    run_bot_add,
)

SECRET = "test-secret"


class FakeRegistry:
    """In-memory BotRegistry; `fail_on_add` simulates a registry write error."""

    def __init__(self, *, fail_on_add: bool = False) -> None:
        self.rows: set[str] = set()
        self.fail_on_add = fail_on_add

    async def contains(self, bot_slug: str) -> bool:
        return bot_slug in self.rows

    async def add(self, bot_slug: str) -> None:
        if self.fail_on_add:
            raise RuntimeError("registry write failed")
        self.rows.add(bot_slug)


class SeqInputs:
    """Scripted input functions raising IndexError when exhausted."""

    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    def __call__(self, prompt: str = "") -> str:
        return self._values.pop(0)


class SeqRng:
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    def choice(self, seq: object) -> str:
        return self._values.pop(0)


def env_files(bots_dir: Path) -> list[str]:
    return sorted(p.name for p in bots_dir.glob(".env-*"))


async def run(
    bots_dir: Path,
    registry: BotRegistry,
    *,
    names: list[str],
    tokens: list[str],
    rng_words: list[str] | None = None,
) -> BotAddResult:
    return await run_bot_add(
        bots_dir=bots_dir,
        registry=registry,
        input_fn=SeqInputs(names),
        token_input_fn=SeqInputs(tokens),
        rng=SeqRng(rng_words) if rng_words else None,
    )


async def test_happy_path_creates_file_and_registry_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bots_dir = tmp_path / "bots"
    registry = FakeRegistry()

    result = await run(
        bots_dir,
        registry,
        names=["Test Bot"],
        tokens=[f"123456789:{SECRET}"],
        rng_words=["arrakis", "melange"],
    )

    assert result is not None
    assert result.bot_slug == "6789-arrakis-melange"
    assert result.name == "Test Bot"
    assert result.token_file == bots_dir / ".env-6789-arrakis-melange"

    # FR-014: directory 0700, file 0600, owner-only
    assert stat.S_IMODE(os.stat(bots_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(result.token_file).st_mode) == 0o600

    # FR-008: the registry holds the slug only; the token lives in the file
    assert registry.rows == {"6789-arrakis-melange"}
    content = result.token_file.read_text(encoding="utf-8")
    assert f"123456789:{SECRET}" in content

    # SC-002 (unit level): the secret never reaches wizard output
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


async def test_invalid_token_is_retried_then_accepted(tmp_path: Path) -> None:
    bots_dir = tmp_path / "bots"
    registry = FakeRegistry()

    result = await run(
        bots_dir,
        registry,
        names=["N"],
        tokens=["no-colon", f"10:{SECRET}"],
        rng_words=["arrakis", "melange"],
    )

    assert result is not None
    assert result.bot_slug.startswith("0010-")
    assert env_files(bots_dir) == [f".env-{result.bot_slug}"]


@pytest.mark.parametrize("bad_name", ["", "   ", "A" * 65, "Bad\x01Name"])
async def test_invalid_name_is_retried(tmp_path: Path, bad_name: str) -> None:
    registry = FakeRegistry()
    result = await run(
        tmp_path / "bots",
        registry,
        names=[bad_name, "Good Name"],
        tokens=[f"10:{SECRET}"],
        rng_words=["arrakis", "melange"],
    )
    assert result is not None
    assert result.name == "Good Name"


async def test_duplicate_token_is_rejected_without_artifacts(
    tmp_path: Path,
) -> None:
    bots_dir = tmp_path / "bots"
    bots_dir.mkdir()
    (bots_dir / ".env-0011-old").write_text(
        f"GRAMMQ_BOT_TOKEN=11:{SECRET}\n", encoding="utf-8"
    )
    registry = FakeRegistry()

    with pytest.raises(DuplicateTokenError):
        await run(bots_dir, registry, names=["N"], tokens=["11:another"])

    assert env_files(bots_dir) == [".env-0011-old"]
    assert registry.rows == set()


async def test_registry_failure_compensates_the_file(tmp_path: Path) -> None:
    bots_dir = tmp_path / "bots"
    registry = FakeRegistry(fail_on_add=True)

    with pytest.raises(RuntimeError, match="registry write failed"):
        await run(
            bots_dir,
            registry,
            names=["N"],
            tokens=[f"10:{SECRET}"],
            rng_words=["arrakis", "melange"],
        )

    # FR-009: the system is back to its initial state
    assert env_files(bots_dir) == []
    assert registry.rows == set()


async def test_interrupt_leaves_no_artifacts(tmp_path: Path) -> None:
    bots_dir = tmp_path / "bots"
    registry = FakeRegistry()

    def interrupting_token_input(prompt: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await run_bot_add(
            bots_dir=bots_dir,
            registry=registry,
            input_fn=SeqInputs(["N"]),
            token_input_fn=interrupting_token_input,
        )

    assert env_files(bots_dir) == []
    assert registry.rows == set()


async def test_slug_taken_on_filesystem_is_regenerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slug_module, "DUNE_WORDS", ("alpha", "beta", "gamma"))
    bots_dir = tmp_path / "bots"
    bots_dir.mkdir()
    (bots_dir / ".env-0010-alpha-beta").write_text("x\n", encoding="utf-8")
    registry = FakeRegistry()

    result = await run(
        bots_dir,
        registry,
        names=["N"],
        tokens=[f"10:{SECRET}"],
        rng_words=["alpha", "beta", "alpha", "gamma"],
    )

    assert result is not None
    assert result.bot_slug == "0010-alpha-gamma"


async def test_slug_taken_in_registry_is_regenerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slug_module, "DUNE_WORDS", ("alpha", "beta", "gamma"))
    registry = FakeRegistry()
    registry.rows.add("0010-alpha-beta")

    result = await run(
        tmp_path / "bots",
        registry,
        names=["N"],
        tokens=[f"10:{SECRET}"],
        rng_words=["alpha", "beta", "alpha", "gamma"],
    )

    assert result is not None
    assert result.bot_slug == "0010-alpha-gamma"


async def test_existing_bots_dir_owned_by_other_user_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bots_dir = tmp_path / "bots"
    bots_dir.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 4242)

    with pytest.raises(BotsDirError, match="другому пользователю"):
        await run(tmp_path / bots_dir.name, FakeRegistry(), names=[], tokens=[])

    assert env_files(bots_dir) == []


async def test_bots_path_that_is_not_a_directory_is_rejected(
    tmp_path: Path,
) -> None:
    not_a_dir = tmp_path / "bots"
    not_a_dir.write_text("file", encoding="utf-8")

    with pytest.raises(BotsDirError, match="не каталог"):
        await run(not_a_dir, FakeRegistry(), names=[], tokens=[])
