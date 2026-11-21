"""Webhook secret handling (spec 007; Principle III).

The webhook secret is a bearer credential presented on every Telegram
update, so it is verified by HMAC-SHA256 digests (microseconds on the hot
path — Argon2id would eat the SC-002 budget and be a CPU-DoS vector),
never stored or compared as plaintext. The system generates the secret in
`gram-mq webhook set`, hands it to Telegram exactly once, and keeps only
the hex digest under the dedicated GRAMMQ_WEBHOOK_MASTER_KEY. Pure
module: no runtime technologies, used by both the API (verification) and
the CLI (generation + storage), like token_store for bot tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from pydantic import SecretStr

#: SHA-256 digest hex length — the width of bots.webhook_secret_hmac.
HMAC_HEX_LENGTH = 64
MASTER_KEY_HEX_LENGTH = 64

# secrets.token_urlsafe(32) yields 43 chars of [A-Za-z0-9_-] — inside the
# charset Telegram's set_webhook secret_token accepts (1-256 chars).
SECRET_ENTROPY_BYTES = 32


def make_webhook_secret() -> str:
    """Generate a webhook secret; exists in plaintext only in CLI memory."""
    return secrets.token_urlsafe(SECRET_ENTROPY_BYTES)


def webhook_secret_hmac(master_key: bytes, secret: str) -> str:
    """hex HMAC-SHA256 of the secret — the stored check value."""
    return hmac.new(master_key, secret.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_webhook_secret(
    master_key: bytes, stored_hex: str, presented: str | None
) -> bool:
    """Constant-time check of a presented header value against the digest.

    None or an empty digest is an outright False (no secret configured for
    the bot — reception off) without touching the HMAC.
    """
    if presented is None or not stored_hex:
        return False
    candidate = webhook_secret_hmac(master_key, presented)
    return hmac.compare_digest(candidate, stored_hex)


def parse_webhook_master_key(value: SecretStr | None) -> bytes | None:
    """SecretStr -> 32 raw bytes; any other shape (unset, bad hex, wrong
    length) is "no key" — one undifferentiated condition for callers,
    mirroring token_store.parse_master_key."""
    if value is None:
        return None
    try:
        key = bytes.fromhex(value.get_secret_value())
    except ValueError:
        return None
    return key if len(key) == MASTER_KEY_HEX_LENGTH // 2 else None
