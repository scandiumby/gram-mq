"""Unit contract of bot_slug generation (spec 002: FR-006, FR-007, SC-005).

Contract (data-model.md): `<last4>-<word1>-<word2>`, last 4 digits of the
numeric token id zero-padded on the left, two distinct Dune words,
slug-safe ASCII, total length <= 30.
"""

from __future__ import annotations

import re

import pytest

import gram_mq.cli.slug as slug_module
from gram_mq.cli.slug import SlugExhaustedError, generate_bot_slug

SLUG_RE = re.compile(r"^[0-9]{4}(-[a-z][a-z-]{0,11}){2}$")


class SeqRng:
    """Scripted rng: returns values in order regardless of the sequence."""

    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    def choice(self, seq: object) -> str:
        return self._values.pop(0)


class CyclingRng:
    """Deterministic rng: cycles through one fixed word pair forever."""

    def __init__(self, values: list[str]) -> None:
        self._values = list(values)
        self._i = 0

    def choice(self, seq: object) -> str:
        value = self._values[self._i % len(self._values)]
        self._i += 1
        return value


def test_slug_format_matches_contract() -> None:
    slug = generate_bot_slug("123456789")
    assert SLUG_RE.match(slug)
    assert len(slug) <= 30
    assert slug.startswith("6789-")


def test_short_token_id_is_zero_padded() -> None:
    assert generate_bot_slug("42").startswith("0042-")


def test_deterministic_rng_is_injected() -> None:
    rng = SeqRng(["arrakis", "melange"])
    assert generate_bot_slug("123456789", rng=rng) == "6789-arrakis-melange"


def test_words_of_pair_are_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the wordlist: without hyphens inside words, split("-") is exact.
    monkeypatch.setattr(slug_module, "DUNE_WORDS", ("alpha", "beta", "gamma"))
    for _ in range(200):
        parts = generate_bot_slug("1111").split("-")
        assert parts[0] == "1111"
        assert parts[1] != parts[2]


def test_sc005_hundred_generations_are_pairwise_distinct() -> None:
    # SC-005: uniqueness among already-issued slugs is maintained by
    # automatic regeneration, so 100 registrations never yield a duplicate.
    seen: set[str] = set()
    for _ in range(100):
        slug = generate_bot_slug("123456789", is_taken=seen.__contains__)
        seen.add(slug)
    assert len(seen) == 100
    assert all(slug.startswith("6789-") for slug in seen)


def test_taken_slug_is_regenerated() -> None:
    rng = SeqRng(["arrakis", "melange", "caladan", "giedi"])
    slug = generate_bot_slug(
        "123456789", is_taken=lambda s: s == "6789-arrakis-melange", rng=rng
    )
    assert slug == "6789-caladan-giedi"


def test_exhaustion_raises_after_fifty_attempts() -> None:
    rng = CyclingRng(["arrakis", "melange"])
    with pytest.raises(SlugExhaustedError):
        generate_bot_slug("1", is_taken=lambda s: True, rng=rng)


def test_non_numeric_token_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="numeric"):
        generate_bot_slug("12a4")
