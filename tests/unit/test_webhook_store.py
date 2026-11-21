"""webhook_store: generation, HMAC check values, constant-time verify."""

from __future__ import annotations

import secrets

from pydantic import SecretStr

from gram_mq.webhook_store import (
    HMAC_HEX_LENGTH,
    make_webhook_secret,
    parse_webhook_master_key,
    verify_webhook_secret,
    webhook_secret_hmac,
)

KEY = secrets.token_bytes(32)


class TestMakeSecret:
    def test_urlsafe_alphabet_and_length(self) -> None:
        secret = make_webhook_secret()
        assert len(secret) == 43  # token_urlsafe(32): 32 bytes -> 43 chars
        assert all(c.isalnum() or c in "_-" for c in secret)

    def test_unique_per_call(self) -> None:
        assert make_webhook_secret() != make_webhook_secret()


class TestHmac:
    def test_hex_digest_length(self) -> None:
        assert len(webhook_secret_hmac(KEY, "secret")) == HMAC_HEX_LENGTH

    def test_deterministic_for_same_key_and_secret(self) -> None:
        assert webhook_secret_hmac(KEY, "s") == webhook_secret_hmac(KEY, "s")

    def test_keyed__different_key_differs(self) -> None:
        other = secrets.token_bytes(32)
        assert webhook_secret_hmac(KEY, "s") != webhook_secret_hmac(other, "s")


class TestVerify:
    def test_roundtrip(self) -> None:
        secret = make_webhook_secret()
        stored = webhook_secret_hmac(KEY, secret)
        assert verify_webhook_secret(KEY, stored, secret)

    def test_wrong_presented_rejected(self) -> None:
        stored = webhook_secret_hmac(KEY, make_webhook_secret())
        assert not verify_webhook_secret(KEY, stored, "forged")

    def test_none_presented_rejected(self) -> None:
        stored = webhook_secret_hmac(KEY, make_webhook_secret())
        assert not verify_webhook_secret(KEY, stored, None)

    def test_empty_stored_rejected(self) -> None:
        assert not verify_webhook_secret(KEY, "", "anything")

    def test_corrupt_stored_hex_rejected(self) -> None:
        # A garbage digest column compares unequal to any candidate digest.
        assert not verify_webhook_secret(KEY, "not-hex!", "anything")


class TestParseMasterKey:
    def test_valid_hex_32_bytes(self) -> None:
        raw = secrets.token_bytes(32)
        assert parse_webhook_master_key(SecretStr(raw.hex())) == raw

    def test_unset_is_none(self) -> None:
        assert parse_webhook_master_key(None) is None

    def test_not_hex_is_none(self) -> None:
        assert parse_webhook_master_key(SecretStr("zz" * 32)) is None

    def test_wrong_length_is_none(self) -> None:
        assert parse_webhook_master_key(SecretStr(secrets.token_bytes(8).hex())) is None
