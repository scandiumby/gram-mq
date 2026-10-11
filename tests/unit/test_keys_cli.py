"""Unit contract of `gram-mq key add` (spec 003: US1, FR-001).

The plaintext key must appear exactly once — in the terminal output —
and never in the stored rows.
"""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher

import gram_mq.cli.main as main_module
from gram_mq.api.auth import KEY_RE, ApiKeyVerifier
from gram_mq.cli.keys import run_key_add

CHEAP_HASHER = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def insert(self, prefix: str, key_hash: str) -> None:
        self.rows[prefix] = key_hash


async def test_key_add_returns_key_and_persists_only_hash() -> None:
    store = FakeStore()

    key, prefix = await run_key_add(insert=store.insert)

    assert KEY_RE.match(key) is not None
    assert prefix == key[:12]
    assert len(store.rows) == 1
    digest = store.rows[prefix]
    assert digest != key
    assert key not in digest
    assert digest.startswith("$argon2id$")


async def test_issued_key_passes_the_verifier() -> None:
    store = FakeStore()
    key, prefix = await run_key_add(insert=store.insert)
    digest = store.rows[prefix]

    async def fetch(p: str) -> tuple[str, bool] | None:
        return (digest, False) if p == prefix else None

    verifier = ApiKeyVerifier(fetch, CHEAP_HASHER)
    assert await verifier.verify(key) is True


async def test_two_keys_differ() -> None:
    store = FakeStore()
    first, _ = await run_key_add(insert=store.insert)
    second, _ = await run_key_add(insert=store.insert)
    assert first != second
    assert len(store.rows) == 2


def test_main_key_add_prints_key_exactly_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "gmq_" + "k" * 43

    async def fake_run_key_add(*, insert: object) -> tuple[str, str]:
        return key, key[:12]

    monkeypatch.setattr(main_module, "run_key_add", fake_run_key_add)
    assert main_module.main(["key", "add"]) == main_module.EXIT_OK

    out = capsys.readouterr().out
    assert out.count(key) == 1
    assert key[:12] in out
    assert "один раз" in out


def test_main_key_add_write_failure_exits_four(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing_run_key_add(*, insert: object) -> tuple[str, str]:
        raise OSError("disk full")

    monkeypatch.setattr(main_module, "run_key_add", failing_run_key_add)
    assert main_module.main(["key", "add"]) == main_module.EXIT_WRITE
    assert "Сбой записи" in capsys.readouterr().err
