"""Unit contract of the split-storage BotConfig adapter (spec 006, FR-012).

tmp bots dir + fake registry lookup: Ready glue, NotRegistered for a
missing row, NotReady for every unreadable-halves condition.
"""

from __future__ import annotations

import os
from pathlib import Path

from gram_mq.token_store import (
    bot_file_path,
    check_bots_dir,
    encrypt_half,
    make_pair_nonce,
    split_token,
    validate_token_format,
    write_bot_file,
)
from gram_mq.worker.bot_config import (
    NotReady,
    NotRegistered,
    Ready,
    SplitFileBotConfig,
)

KEY = os.urandom(32)
OTHER_KEY = os.urandom(32)
TOKEN = "123456789:" + "s" * 32
SLUG = "6789-alpha-beta"


def store_pair(
    bots_dir: Path,
    slug: str,
    token: str = TOKEN,
    key: bytes = KEY,
) -> tuple[str, str]:
    """Write the file half; return (db_blob, nonce_hex) for the fake row."""
    token_id, secret = validate_token_format(token)
    file_plain, db_plain = split_token(token_id, secret)
    nonce = make_pair_nonce()
    write_bot_file(
        bot_file_path(bots_dir, slug),
        name="Test",
        file_blob=encrypt_half(key, nonce, file_plain),
        pair_nonce=nonce.hex(),
    )
    return encrypt_half(key, nonce, db_plain), nonce.hex()


def make_config(
    bots_dir: Path,
    row: tuple[str, str] | None,
    *,
    master_key: bytes | None = KEY,
) -> SplitFileBotConfig:
    async def fetch_half(slug: str) -> tuple[str, str] | None:
        assert slug == SLUG
        return row

    return SplitFileBotConfig(
        bots_dir=bots_dir, fetch_half=fetch_half, master_key=master_key
    )


async def test_ready_glues_the_full_token(tmp_path: Path) -> None:
    check_bots_dir(tmp_path)
    row = store_pair(tmp_path, SLUG)
    config = make_config(tmp_path, row)
    assert await config.token_for(SLUG) == Ready(TOKEN)


async def test_missing_registry_row_is_not_registered(tmp_path: Path) -> None:
    check_bots_dir(tmp_path)
    store_pair(tmp_path, SLUG)  # even with a file present
    assert await make_config(tmp_path, None).token_for(SLUG) == NotRegistered()


async def test_missing_file_is_not_ready(tmp_path: Path) -> None:
    row = store_pair(tmp_path, SLUG)
    bot_file_path(tmp_path, SLUG).unlink()
    assert await make_config(tmp_path, row).token_for(SLUG) == NotReady()


async def test_legacy_row_with_null_halves_is_not_ready(tmp_path: Path) -> None:
    assert await make_config(tmp_path, ("", "")).token_for(SLUG) == NotReady()


async def test_missing_master_key_is_not_ready(tmp_path: Path) -> None:
    check_bots_dir(tmp_path)
    row = store_pair(tmp_path, SLUG)
    assert (
        await make_config(tmp_path, row, master_key=None).token_for(SLUG) == NotReady()
    )


async def test_wrong_master_key_is_not_ready(tmp_path: Path) -> None:
    check_bots_dir(tmp_path)
    row = store_pair(tmp_path, SLUG)
    assert (
        await make_config(tmp_path, row, master_key=OTHER_KEY).token_for(SLUG)
        == NotReady()
    )


async def test_tampered_file_blob_is_not_ready(tmp_path: Path) -> None:
    check_bots_dir(tmp_path)
    row = store_pair(tmp_path, SLUG)
    path = bot_file_path(tmp_path, SLUG)
    text = path.read_text(encoding="utf-8")
    # Flip one hex pair inside the blob line without breaking the format.
    lines = text.splitlines()
    blob = lines[1]
    damaged = blob[:-4] + ("0000" if blob[-4:] != "0000" else "0101")
    lines[1] = damaged
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert await make_config(tmp_path, row).token_for(SLUG) == NotReady()


async def test_generation_mismatch_nonce_is_not_ready(tmp_path: Path) -> None:
    # A new file generation (fresh nonce) glued to the old table row:
    # the pairing nonce differs — decrypt refuses locally (FR-009).
    check_bots_dir(tmp_path)
    old_row = store_pair(tmp_path, SLUG)
    new_row = store_pair(tmp_path, SLUG)  # overwrote the file, new nonce
    assert new_row[1] != old_row[1]
    assert await make_config(tmp_path, old_row).token_for(SLUG) == NotReady()


async def test_swapped_halves_between_bots_are_not_ready(tmp_path: Path) -> None:
    # File half of bot A (TOKEN) + table half of bot B (OTHER): the
    # pairing nonces differ -> decryption refuses the mix-and-match glue.
    check_bots_dir(tmp_path)
    store_pair(tmp_path, SLUG)  # the file keeps TOKEN's half
    other_id, other_secret = validate_token_format("987654321:" + "q" * 32)
    _, other_db_plain = split_token(other_id, other_secret)
    nonce_b = make_pair_nonce()
    row_b = (encrypt_half(KEY, nonce_b, other_db_plain), nonce_b.hex())
    assert await make_config(tmp_path, row_b).token_for(SLUG) == NotReady()


async def test_damaged_file_is_not_ready(tmp_path: Path) -> None:
    check_bots_dir(tmp_path)
    row = store_pair(tmp_path, SLUG)
    bot_file_path(tmp_path, SLUG).write_text("garbage\n", encoding="utf-8")
    assert await make_config(tmp_path, row).token_for(SLUG) == NotReady()
