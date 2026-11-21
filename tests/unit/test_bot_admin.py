"""Unit contract of bot provisioning (spec 006, US1; R7/R8).

Fake registry + tmp bots dir: dedup by decrypted file halves, healing of
an interrupted pair under the original slug, slug uniqueness, encrypted
storage invariants, and the revoke flow.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gram_mq.bot_admin import (
    DuplicateTokenError,
    provision_bot,
    revoke_bot,
)
from gram_mq.slug import SlugRng
from gram_mq.token_store import (
    bot_file_path,
    check_bots_dir,
    decrypt_half,
    read_bot_file,
)

KEY = os.urandom(32)
TOKEN = "123456789:" + "a" * 32
OTHER_TOKEN = "987654321:" + "b" * 32


class SeqRng:
    """Deterministic slug words: word0/word1 then word2/word3..."""

    def __init__(self) -> None:
        self._state = 0

    def choice(self, seq: Sequence[str]) -> str:
        value = seq[self._state % len(seq)]
        self._state += 1
        return value


@dataclass
class FakeRegistry:
    rows: dict[str, tuple[str, str]] = field(default_factory=dict)

    async def contains(self, bot_slug: str) -> bool:
        return bot_slug in self.rows

    async def add(self, bot_slug: str, *, token_half: str, pair_nonce: str) -> None:
        if bot_slug in self.rows:
            raise RuntimeError("unique violation")
        self.rows[bot_slug] = (token_half, pair_nonce)

    async def fetch_half(self, bot_slug: str) -> tuple[str, str] | None:
        return self.rows.get(bot_slug)

    async def delete(self, bot_slug: str) -> bool:
        return self.rows.pop(bot_slug, None) is not None


@pytest.fixture
def bots_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bots"
    check_bots_dir(directory)
    return directory


async def provision(
    bots_dir: Path,
    registry: FakeRegistry,
    token: str = TOKEN,
    name: str = "Test Bot",
    rng: SlugRng | None = None,
) -> str:
    return await provision_bot(
        bots_dir=bots_dir,
        registry=registry,
        master_key=KEY,
        token=token,
        name=name,
        rng=rng,
    )


class TestProvision:
    async def test_creates_file_and_row_with_encrypted_halves(
        self, bots_dir: Path
    ) -> None:
        registry = FakeRegistry()
        slug = await provision(bots_dir, registry)

        assert slug in registry.rows
        record = read_bot_file(bot_file_path(bots_dir, slug))
        assert record is not None
        assert record.name == "Test Bot"
        # The stored file plaintext is the id + 4 secret chars, encrypted.
        aad = bytes.fromhex(record.pair_nonce)
        file_plain = decrypt_half(KEY, aad, record.file_blob)
        db_plain = decrypt_half(KEY, aad, registry.rows[slug][0])
        assert file_plain is not None and db_plain is not None
        assert file_plain + db_plain == TOKEN
        assert TOKEN not in bot_file_path(bots_dir, slug).read_text(encoding="utf-8")

    async def test_slug_follows_token_id_last4(self, bots_dir: Path) -> None:
        slug = await provision(bots_dir, FakeRegistry(), rng=SeqRng())
        assert slug.startswith("6789-")

    async def test_same_token_twice_conflicts_for_a_live_bot(
        self, bots_dir: Path
    ) -> None:
        registry = FakeRegistry()
        await provision(bots_dir, registry)
        with pytest.raises(DuplicateTokenError):
            await provision(bots_dir, registry)

    async def test_other_token_registers_independently(self, bots_dir: Path) -> None:
        registry = FakeRegistry()
        first = await provision(bots_dir, registry, rng=SeqRng())
        second = await provision(bots_dir, registry, token=OTHER_TOKEN, rng=SeqRng())
        assert first != second
        assert len(registry.rows) == 2

    async def test_interrupted_pair_is_healed_under_original_slug(
        self, bots_dir: Path
    ) -> None:
        registry = FakeRegistry()
        slug = await provision(bots_dir, registry, rng=SeqRng())
        # Simulate an interrupted creation: the file survives, the row is
        # gone (crash between file write and registry insert).
        orphan_pair = registry.rows.pop(slug)

        healed_slug = await provision(bots_dir, registry, rng=SeqRng())

        assert healed_slug == slug  # same slug, not a duplicate bot
        assert registry.rows[slug] != orphan_pair  # fresh halves + nonce
        record = read_bot_file(bot_file_path(bots_dir, slug))
        assert record is not None
        aad = bytes.fromhex(record.pair_nonce)
        secret_part = TOKEN.partition(":")[2]
        assert decrypt_half(KEY, aad, registry.rows[slug][0]) == secret_part[4:]

    async def test_slug_collision_with_existing_bot_regenerates(
        self, bots_dir: Path
    ) -> None:
        registry = FakeRegistry()
        # Same last-4 digits and the same rng sequence: the first slug
        # candidate collides with the live bot, the next one must not.
        same_last4 = "223456789:" + "c" * 32
        first = await provision(bots_dir, registry, rng=SeqRng())
        second = await provision(bots_dir, registry, token=same_last4, rng=SeqRng())
        assert first != second
        assert first.startswith("6789-") and second.startswith("6789-")
        assert len(registry.rows) == 2


class TestRevoke:
    async def test_revoke_removes_row_and_file(self, bots_dir: Path) -> None:
        registry = FakeRegistry()
        slug = await provision(bots_dir, registry, rng=SeqRng())
        assert await revoke_bot(bots_dir=bots_dir, registry=registry, bot_slug=slug)
        assert slug not in registry.rows
        assert not bot_file_path(bots_dir, slug).exists()

    async def test_revoke_unknown_slug_is_false(self, bots_dir: Path) -> None:
        assert not await revoke_bot(
            bots_dir=bots_dir, registry=FakeRegistry(), bot_slug="0000-x-y"
        )

    async def test_provision_after_revoke_registers_again(self, bots_dir: Path) -> None:
        registry = FakeRegistry()
        slug = await provision(bots_dir, registry, rng=SeqRng())
        await revoke_bot(bots_dir=bots_dir, registry=registry, bot_slug=slug)
        again = await provision(bots_dir, registry, rng=SeqRng())
        assert again in registry.rows
