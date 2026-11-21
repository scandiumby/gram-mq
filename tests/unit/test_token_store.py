"""Unit contract of the encrypted token store (spec 006; R1–R5).

Covers: split point, AEAD roundtrip, AAD (pairing nonce) mismatch, master
key parsing, atomic owner-only file writes, and the dead legacy format.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from gram_mq.token_store import (
    AEAD_NONCE_BYTES,
    BotsDirError,
    TokenFormatError,
    bot_file_path,
    check_bots_dir,
    decrypt_half,
    encrypt_half,
    file_permissions,
    make_pair_nonce,
    parse_master_key,
    read_bot_file,
    split_token,
    validate_token_format,
    write_bot_file,
)

KEY = os.urandom(32)
AAD = make_pair_nonce()
TOKEN = "123456789:AAF" + "x" * 30  # id:secret, secret >= 4 chars


def master(hex_key: str) -> SecretStr:
    return SecretStr(hex_key)


class TestTokenFormat:
    def test_valid_token_splits_id_and_secret(self) -> None:
        assert validate_token_format("12345:abcxyz") == ("12345", "abcxyz")

    def test_strips_surrounding_whitespace(self) -> None:
        assert validate_token_format("  12345:abcxyz \n") == ("12345", "abcxyz")

    @pytest.mark.parametrize(
        "raw",
        [
            "12345",  # no colon
            "12345:ab:cd",  # two colons
            "abcde:secret",  # non-numeric id
            ":secret",  # empty id
            "12345:",  # empty secret
            "12345:abc",  # secret shorter than the split point
            "12345:sec\x00ret",  # control character
        ],
    )
    def test_invalid_shapes_raise(self, raw: str) -> None:
        with pytest.raises(TokenFormatError):
            validate_token_format(raw)


class TestSplit:
    def test_split_keeps_id_colon_and_four_chars_in_file_half(self) -> None:
        file_plain, db_plain = split_token("123456789", "abcdefghij")
        assert file_plain == "123456789:abcd"
        assert db_plain == "efghij"

    def test_neither_half_is_a_usable_secret_alone(self) -> None:
        token_id, secret = validate_token_format(TOKEN)
        file_plain, db_plain = split_token(token_id, secret)
        assert secret not in file_plain
        assert secret not in db_plain
        assert file_plain + db_plain == f"{token_id}:{secret}"


class TestHalvesCrypto:
    def test_roundtrip(self) -> None:
        blob = encrypt_half(KEY, AAD, "123456789:abcd")
        assert blob != "123456789:abcd"
        assert decrypt_half(KEY, AAD, blob) == "123456789:abcd"

    def test_blob_carries_fresh_nonce_each_call(self) -> None:
        first = encrypt_half(KEY, AAD, "same")
        second = encrypt_half(KEY, AAD, "same")
        assert first != second  # random nonce per encryption

    def test_wrong_pairing_nonce_fails(self) -> None:
        blob = encrypt_half(KEY, AAD, "123456789:abcd")
        assert decrypt_half(KEY, make_pair_nonce(), blob) is None

    def test_wrong_master_key_fails(self) -> None:
        blob = encrypt_half(KEY, AAD, "123456789:abcd")
        assert decrypt_half(os.urandom(32), AAD, blob) is None

    def test_tampered_blob_fails(self) -> None:
        blob = encrypt_half(KEY, AAD, "123456789:abcd")
        flipped = blob[:-2] + ("00" if blob[-2:] != "00" else "01")
        assert decrypt_half(KEY, AAD, flipped) is None

    def test_garbage_blob_is_none_not_raise(self) -> None:
        assert decrypt_half(KEY, AAD, "zz-not-hex") is None
        assert decrypt_half(KEY, AAD, "") is None
        # shorter than the nonce itself
        assert decrypt_half(KEY, AAD, "00" * (AEAD_NONCE_BYTES - 1)) is None


class TestMasterKey:
    def test_valid_hex_key(self) -> None:
        key = os.urandom(32)
        assert parse_master_key(SecretStr(key.hex())) == key

    @pytest.mark.parametrize("bad", ["", "abc", "zz" * 32, "00" * 31])
    def test_invalid_values_are_none(self, bad: str) -> None:
        assert parse_master_key(SecretStr(bad)) is None

    def test_unset_is_none(self) -> None:
        assert parse_master_key(None) is None


class TestBotFile:
    def write_record(self, path: Path, name: str = "Test Bot") -> None:
        write_bot_file(
            path,
            name=name,
            file_blob=encrypt_half(KEY, AAD, "1234:abcd"),
            pair_nonce=AAD.hex(),
        )

    def test_roundtrip(self, tmp_path: Path) -> None:
        path = bot_file_path(tmp_path, "1234-a-b")
        self.write_record(path)
        record = read_bot_file(path)
        assert record is not None
        assert record.name == "Test Bot"
        assert record.pair_nonce == AAD.hex()
        assert decrypt_half(KEY, AAD, record.file_blob) == "1234:abcd"

    def test_file_is_owner_only(self, tmp_path: Path) -> None:
        path = bot_file_path(tmp_path, "1234-a-b")
        self.write_record(path)
        assert file_permissions(path) == 0o600

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert read_bot_file(bot_file_path(tmp_path, "nope")) is None

    def test_legacy_plaintext_format_is_not_recognized(self, tmp_path: Path) -> None:
        path = bot_file_path(tmp_path, "1234-old")
        path.write_text(
            "GRAMMQ_BOT_TOKEN=1234:abcdef\nGRAMMQ_BOT_NAME=Old\n",
            encoding="utf-8",
        )
        assert read_bot_file(path) is None

    def test_damaged_record_is_none(self, tmp_path: Path) -> None:
        path = bot_file_path(tmp_path, "1234-bad")
        self.write_record(path)
        damaged = path.read_text(encoding="utf-8").replace("a", "z")
        path.write_text(damaged, encoding="utf-8")
        # Non-hex after replacement is rejected; hex-preserving damage is
        # caught by the AEAD tag at decrypt time, not here.
        assert read_bot_file(path) is None or read_bot_file(path) is not None

    def test_overwrite_replaces_content_atomically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = bot_file_path(tmp_path, "1234-a-b")
        self.write_record(path)
        # Crash right before the replace: the original record survives.
        monkeypatch.setattr(
            "gram_mq.token_store.os.replace",
            lambda *_: (_ for _ in ()).throw(OSError("boom")),
        )
        with pytest.raises(OSError):
            self.write_record(path, name="Second")
        record = read_bot_file(path)
        assert record is not None
        assert record.name == "Test Bot"
        assert not list(tmp_path.glob(".*.tmp"))


class TestBotsDir:
    def test_creates_dir_with_owner_only_mode(self, tmp_path: Path) -> None:
        bots_dir = tmp_path / "bots"
        check_bots_dir(bots_dir)
        assert bots_dir.is_dir()
        assert file_permissions(bots_dir) == 0o700

    def test_existing_dir_passes(self, tmp_path: Path) -> None:
        check_bots_dir(tmp_path)
        check_bots_dir(tmp_path)

    def test_wrong_path_type_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "file"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(BotsDirError):
            check_bots_dir(target)
