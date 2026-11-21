"""Encrypted split storage of bot tokens (spec 006; Principle III v3.0.0).

The token never exists whole at rest: the file half (numeric id + ":" +
the first 4 characters of the secret part) lives in `bots/.env-<slug>`,
the table half (the rest of the secret) in the bots table row. Both
halves are AESGCM-encrypted under the dedicated master key; the AEAD
associated data is the 8-byte pairing nonce stored in the clear with each
half, so halves of different bots or of different generations fail
decryption locally, before any Telegram call. Pure module: no runtime
technologies, used by both the API (encrypt on provisioning) and the
worker (decrypt on delivery).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

NAME_KEY = "GRAMMQ_BOT_NAME"
FILE_HALF_KEY = "GRAMMQ_BOT_FILE_HALF"
PAIR_NONCE_KEY = "GRAMMQ_BOT_PAIR_NONCE"

# The split point of the secret part (spec 006, R4): the file half keeps
# the first 4 characters, the table half the rest. Neither store alone
# yields a usable secret even under a leaked master key.
FILE_SECRET_CHARS = 4

AEAD_NONCE_BYTES = 12
PAIR_NONCE_BYTES = 8
MASTER_KEY_HEX_LENGTH = 64


class TokenFormatError(ValueError):
    """Invalid `{id}:{secret}` shape; the message never quotes the token."""


class BotsDirError(RuntimeError):
    """bots/ is unusable: wrong path type or another user's directory."""


@dataclass(frozen=True, slots=True)
class BotFileRecord:
    """Parsed `bots/.env-<slug>` in the encrypted format."""

    name: str
    file_blob: str  # hex(nonce ‖ ciphertext‖tag)
    pair_nonce: str  # hex, shared with the table half


def validate_token_format(raw: str) -> tuple[str, str]:
    """Return (id, secret) or raise TokenFormatError (contract 002 + R4)."""
    candidate = raw.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        raise TokenFormatError("токен содержит управляющие символы или переводы строки")
    token_id, sep, secret = candidate.partition(":")
    if not sep or ":" in secret:
        raise TokenFormatError("токен должен содержать ровно один разделитель ':'")
    if not token_id.isdigit():
        raise TokenFormatError("идентификатор токена должен быть числом")
    if len(secret) < FILE_SECRET_CHARS:
        raise TokenFormatError(
            f"секретная часть токена короче {FILE_SECRET_CHARS} символов"
        )
    return token_id, secret


def split_token(token_id: str, secret: str) -> tuple[str, str]:
    """(file_plain, db_plain): `"{id}:{secret[:4]}"` and `secret[4:]`."""
    return f"{token_id}:{secret[:FILE_SECRET_CHARS]}", secret[FILE_SECRET_CHARS:]


def make_pair_nonce() -> bytes:
    return os.urandom(PAIR_NONCE_BYTES)


def parse_master_key(value: SecretStr | None) -> bytes | None:
    """SecretStr -> 32 raw bytes; any other shape (unset, bad hex, wrong
    length) is "no key" — one undifferentiated condition for callers."""
    if value is None:
        return None
    try:
        key = bytes.fromhex(value.get_secret_value())
    except ValueError:
        return None
    return key if len(key) == MASTER_KEY_HEX_LENGTH // 2 else None


def encrypt_half(key: bytes, aad: bytes, plain: str) -> str:
    """hex(nonce ‖ AESGCM(nonce, plain, aad)); fresh nonce per call."""
    nonce = os.urandom(AEAD_NONCE_BYTES)
    blob = AESGCM(key).encrypt(nonce, plain.encode("utf-8"), aad)
    return (nonce + blob).hex()


def decrypt_half(key: bytes, aad: bytes, blob: str) -> str | None:
    """Reverse of encrypt_half; None on any tamper/mismatch (InvalidTag),
    wrong hex shape, or decode failure — never raises to the caller."""
    try:
        raw = bytes.fromhex(blob)
        head, body = raw[:AEAD_NONCE_BYTES], raw[AEAD_NONCE_BYTES:]
        if not head:
            return None
        plain = AESGCM(key).decrypt(head, body, aad)
        return plain.decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError):
        return None


def check_bots_dir(bots_dir: Path) -> None:
    """Owner-only 0700 directory, created if absent (wizard heritage)."""
    if bots_dir.exists():
        if not bots_dir.is_dir():
            raise BotsDirError(f"{bots_dir} не каталог")
        if bots_dir.stat().st_uid != os.geteuid():
            raise BotsDirError(f"каталог {bots_dir} принадлежит другому пользователю")
    else:
        bots_dir.mkdir(mode=0o700)
        os.chmod(bots_dir, 0o700)


def bot_file_path(bots_dir: Path, bot_slug: str) -> Path:
    return bots_dir / f".env-{bot_slug}"


def write_bot_file(path: Path, *, name: str, file_blob: str, pair_nonce: str) -> None:
    """Atomic owner-only write: tmp file 0600 + fsync + os.replace, so a
    crash mid-write never leaves a half-written record and provisioning
    can overwrite a partial pair from an interrupted run (R5)."""
    content = (
        f"{NAME_KEY}={name}\n"
        f"{FILE_HALF_KEY}={file_blob}\n"
        f"{PAIR_NONCE_KEY}={pair_nonce}\n"
    )
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_bot_file(path: Path) -> BotFileRecord | None:
    """Parse the encrypted format; None when missing, unreadable, damaged,
    or in the removed plaintext format (`GRAMMQ_BOT_TOKEN` is simply not a
    recognized key here — FR-014: the legacy branch must stay dead)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    name = values.get(NAME_KEY, "")
    file_blob = values.get(FILE_HALF_KEY, "")
    pair_nonce = values.get(PAIR_NONCE_KEY, "")
    if not name or not file_blob or not pair_nonce:
        return None
    try:
        bytes.fromhex(file_blob)
        nonce = bytes.fromhex(pair_nonce)
    except ValueError:
        return None
    if len(nonce) != PAIR_NONCE_BYTES:
        return None
    return BotFileRecord(name=name, file_blob=file_blob, pair_nonce=pair_nonce)


def file_permissions(path: Path) -> int:
    """Test helper: the raw permission bits (0600 expected)."""
    return stat.S_IMODE(path.stat().st_mode)
