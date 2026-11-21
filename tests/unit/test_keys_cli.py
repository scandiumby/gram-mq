"""Unit contract of `gram-mq key add` (spec 003: US1, FR-001) and
`gram-mq key prepare` (spec 005: US1, FR-001–FR-004).

The plaintext key must appear exactly once — in the terminal output —
and never in the stored rows. `key prepare` additionally works with no
database and no settings at all (FR-002).
"""

from __future__ import annotations

import re

import pytest
from argon2 import PasswordHasher

import gram_mq.cli.main as main_module
from gram_mq.api.auth import KEY_RE, ApiKeyVerifier, parse_key_material
from gram_mq.cli.keys import run_key_add, run_key_prepare
from gram_mq.cli.main import EXIT_OK, main

CHEAP_HASHER = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)

MATERIAL_RE = re.compile(r"^(gmq_[A-Za-z0-9_-]{8}:\$argon2id\$.+)$", re.MULTILINE)


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


def test_key_prepare_is_a_plain_sync_function() -> None:
    # FR-002: no DB, no awaitables — the whole stage 1 must run on a
    # machine with nothing but the project environment.
    result = run_key_prepare()
    assert isinstance(result, tuple)


def test_key_prepare_material_round_trips_and_verifies() -> None:
    key, prefix, material = run_key_prepare()

    assert KEY_RE.match(key) is not None
    assert prefix == key[:12]
    assert material.startswith(f"{prefix}:$argon2id$")
    assert parse_key_material(material) == (prefix, material.split(":", 1)[1])
    # The hash must verify against the plaintext with the production
    # verifier's hasher (US1 scenario 4: format compatibility).
    digest = material.split(":", 1)[1]
    assert key not in digest
    assert CHEAP_HASHER.verify(digest, key) is True


def test_two_prepared_keys_differ() -> None:
    first = run_key_prepare()
    second = run_key_prepare()
    assert first[0] != second[0]
    assert first[2] != second[2]


def test_main_key_prepare_works_without_database_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FR-002: no GRAMMQ_* environment at all — the command must not
    # instantiate Settings or touch the DB.
    for name in ("GRAMMQ_DATABASE_URL", "GRAMMQ_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)

    assert main(["key", "prepare"]) == EXIT_OK

    out = capsys.readouterr().out
    match = re.search(rf"({KEY_RE.pattern[1:-1]})", out)
    assert match is not None, "plaintext key must be printed"
    key = match.group(1)
    assert out.count(key) == 1, "plaintext key appears exactly once"
    assert f"prefix: {key[:12]}" in out
    material_match = MATERIAL_RE.search(out)
    assert material_match is not None, "activation material must be printed"
    material = material_match.group(1)
    assert parse_key_material(material) is not None
    assert "GRAMMQ_KEY_BOOTSTRAP_MATERIAL" in out
