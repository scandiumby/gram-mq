"""Bot token format and the `bots/.env-<bot_slug>` file (spec 002, FR-005/FR-008).

Format contract (data-model.md): `{numeric_id}:{secret}` with exactly one
colon, non-empty numeric id, non-empty secret without control characters.
The file holds `GRAMMQ_BOT_TOKEN` and `GRAMMQ_BOT_NAME` in dotenv style and
is created exclusively with owner-only permissions.
"""

from __future__ import annotations

import os
from pathlib import Path

TOKEN_KEY = "GRAMMQ_BOT_TOKEN"
NAME_KEY = "GRAMMQ_BOT_NAME"


class TokenFormatError(ValueError):
    """Invalid `{id}:{secret}` shape; the message never quotes the token."""


def validate_token_format(raw: str) -> tuple[str, str]:
    """Return (id, secret) or raise TokenFormatError (FR-005)."""
    candidate = raw.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        raise TokenFormatError("токен содержит управляющие символы или переводы строки")
    token_id, sep, secret = candidate.partition(":")
    if not sep or ":" in secret:
        raise TokenFormatError("токен должен содержать ровно один разделитель ':'")
    if not token_id.isdigit():
        raise TokenFormatError("идентификатор токена должен быть числом")
    if not secret:
        raise TokenFormatError("секретная часть токена пуста")
    return token_id, secret


def write_token_file(path: Path, token: str, name: str) -> None:
    """Create the config file atomically: O_EXCL, 0600, fsync (research R7)."""
    content = f"{TOKEN_KEY}={token}\n{NAME_KEY}={name}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def read_token_file(path: Path) -> tuple[str, str] | None:
    """Parse a config file; None when missing, unreadable or damaged."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    token: str | None = None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == TOKEN_KEY:
            token = value.strip()
            break
    if token is None:
        return None
    try:
        return validate_token_format(token)
    except TokenFormatError:
        return None


def collect_registered_token_ids(bots_dir: Path) -> set[str]:
    """Token ids of all readable `bots/.env-*` files (FR-012 file scan).

    Damaged or unparsable files are skipped: they mark their bot as not
    ready rather than blocking registration.
    """
    ids: set[str] = set()
    for path in sorted(bots_dir.glob(".env-*")):
        parsed = read_token_file(path)
        if parsed is not None:
            ids.add(parsed[0])
    return ids
