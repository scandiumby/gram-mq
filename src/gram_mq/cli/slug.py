"""bot_slug generation (spec 002: FR-006, FR-007; data-model.md contract).

`<last4>-<word1>-<word2>`: the last 4 digits of the numeric token id,
zero-padded on the left, plus two distinct words from the Dune wordlist.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Sequence
from typing import Protocol

from gram_mq.cli.dune_words import DUNE_WORDS

MAX_ATTEMPTS = 50
SLUG_RE = re.compile(r"^[0-9]{4}(-[a-z][a-z-]{0,11}){2}$")


class SlugExhaustedError(RuntimeError):
    """No unique slug was produced within MAX_ATTEMPTS attempts."""


class SlugRng(Protocol):
    """The only rng capability slug generation needs."""

    def choice(self, seq: Sequence[str]) -> str: ...


def _four_digits(token_id: str) -> str:
    if not token_id.isdigit():
        raise ValueError(f"token id must be numeric, got {token_id!r} shape")
    return token_id[-4:].zfill(4)


def _word_pair(rng: SlugRng) -> tuple[str, str]:
    first = rng.choice(DUNE_WORDS)
    second = rng.choice([word for word in DUNE_WORDS if word != first])
    return first, second


def generate_bot_slug(
    token_id: str,
    *,
    is_taken: Callable[[str], bool] | None = None,
    rng: SlugRng | None = None,
) -> str:
    """Generate a unique human-readable slug for a numeric token id.

    `is_taken` marks candidates as unavailable (registry row or an
    existing `bots/.env-<slug>` path); the default marks nothing.
    """
    taken = is_taken or (lambda _slug: False)
    chooser = rng or random.SystemRandom()
    digits = _four_digits(token_id)
    for _ in range(MAX_ATTEMPTS):
        first, second = _word_pair(chooser)
        slug = f"{digits}-{first}-{second}"
        if SLUG_RE.match(slug) is not None and not taken(slug):
            return slug
    raise SlugExhaustedError(
        f"no unique slug for prefix {digits} within {MAX_ATTEMPTS} attempts"
    )
