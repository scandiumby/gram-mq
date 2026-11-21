"""Bot provisioning and revocation over the registry port (spec 006).

Orchestrates the pure `token_store` crypto core, the slug rules, and the
registry: dedup by decrypted file halves, healing of interrupted pairs,
file-then-row ordering, and the hard revoke. Pure orchestration — no
runtime technologies, testable on fakes (Constitution I).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Protocol

from gram_mq.slug import SlugExhaustedError, SlugRng, generate_bot_slug
from gram_mq.token_store import (
    bot_file_path,
    check_bots_dir,
    decrypt_half,
    encrypt_half,
    make_pair_nonce,
    read_bot_file,
    split_token,
    validate_token_format,
    write_bot_file,
)

REGISTRATION_ATTEMPTS = 50


class DuplicateTokenError(RuntimeError):
    """The token id is already registered behind a live bot (FR-009)."""


class BotRegistry(Protocol):
    """The registry surface provisioning needs (wizard heritage)."""

    async def contains(self, bot_slug: str) -> bool: ...

    async def add(self, bot_slug: str, *, token_half: str, pair_nonce: str) -> None: ...


class RevocableRegistry(BotRegistry, Protocol):
    async def fetch_half(self, bot_slug: str) -> tuple[str, str] | None: ...

    async def delete(self, bot_slug: str) -> bool: ...


async def provision_bot(
    *,
    bots_dir: Path,
    registry: BotRegistry,
    master_key: bytes,
    token: str,
    name: str,
    rng: SlugRng | None = None,
) -> str:
    """Create (or heal) a bot; returns its slug.

    Order of writes: file half first, registry row second — a crash in
    between leaves a healable partial pair (FR-009), never a live bot
    without storage. Raises TokenFormatError (bad token),
    DuplicateTokenError (live bot with the same token id),
    SlugExhaustedError, BotsDirError.
    """
    token_id, secret = validate_token_format(token)
    file_plain, db_plain = split_token(token_id, secret)
    check_bots_dir(bots_dir)
    chooser = rng or random.SystemRandom()

    healed = await _heal_partial_pair(
        bots_dir=bots_dir,
        registry=registry,
        master_key=master_key,
        token_id=token_id,
        file_plain=file_plain,
        db_plain=db_plain,
        name=name,
    )
    if healed is not None:
        return healed

    for _ in range(REGISTRATION_ATTEMPTS):
        slug = generate_bot_slug(
            token_id,
            is_taken=lambda candidate: bot_file_path(bots_dir, candidate).exists(),
            rng=chooser,
        )
        if await registry.contains(slug):
            continue
        db_blob, nonce_hex = _write_file_half(
            bots_dir,
            slug,
            master_key=master_key,
            file_plain=file_plain,
            db_plain=db_plain,
            name=name,
        )
        try:
            await registry.add(slug, token_half=db_blob, pair_nonce=nonce_hex)
        except BaseException:
            # Lost the uniqueness race (or the DB hiccupped): drop the
            # half-written file so the next attempt starts clean.
            bot_file_path(bots_dir, slug).unlink(missing_ok=True)
            raise
        return slug
    raise SlugExhaustedError(
        f"не удалось подобрать уникальный slug за {REGISTRATION_ATTEMPTS} попыток"
    )


async def revoke_bot(
    *,
    bots_dir: Path,
    registry: RevocableRegistry,
    bot_slug: str,
) -> bool:
    """Hard revoke: registry rows in one transaction, then the file half.

    A failed unlink does not roll the revoke back — an orphaned file is
    re-intercepted by provisioning's dedup scan as a partial pair and
    overwritten. False when the slug was already gone.
    """
    if not await registry.delete(bot_slug):
        return False
    bot_file_path(bots_dir, bot_slug).unlink(missing_ok=True)
    return True


async def _heal_partial_pair(
    *,
    bots_dir: Path,
    registry: BotRegistry,
    master_key: bytes,
    token_id: str,
    file_plain: str,
    db_plain: str,
    name: str,
) -> str | None:
    """Dedup scan: match the new token id against decrypted file halves.

    A live registration (file + row) means a duplicate; a file without a
    row is the debris of an interrupted creation — both halves are
    rewritten fresh under the original slug and the row is added.
    """
    # A µs-scale directory scan of small files (tens of bots); no
    # network or DB — to_thread would add noise, not safety.
    for path in sorted(bots_dir.glob(".env-*")):  # noqa: ASYNC240
        record = read_bot_file(path)
        if record is None:
            continue
        aad = bytes.fromhex(record.pair_nonce)
        plain = decrypt_half(master_key, aad, record.file_blob)
        if plain is None:
            continue  # foreign key or damaged: not this token's business
        existing_id = plain.partition(":")[0]
        if existing_id != token_id:
            continue
        slug = path.name.removeprefix(".env-")
        if await registry.contains(slug):
            raise DuplicateTokenError(
                "токен с таким идентификатором уже зарегистрирован"
            )
        db_blob, nonce_hex = _write_file_half(
            bots_dir,
            slug,
            master_key=master_key,
            file_plain=file_plain,
            db_plain=db_plain,
            name=name,
        )
        await registry.add(slug, token_half=db_blob, pair_nonce=nonce_hex)
        return slug
    return None


def _write_file_half(
    bots_dir: Path,
    slug: str,
    *,
    master_key: bytes,
    file_plain: str,
    db_plain: str,
    name: str,
) -> tuple[str, str]:
    """Write the encrypted file half with a fresh pairing nonce; returns
    (db_blob, pair_nonce_hex) — the table half is encrypted under the
    same nonce so the pair is bound together as AEAD associated data."""
    pair_nonce = make_pair_nonce()
    write_bot_file(
        bot_file_path(bots_dir, slug),
        name=name,
        file_blob=encrypt_half(master_key, pair_nonce, file_plain),
        pair_nonce=pair_nonce.hex(),
    )
    return encrypt_half(master_key, pair_nonce, db_plain), pair_nonce.hex()
