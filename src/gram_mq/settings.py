"""Project settings: the single configuration channel.

Engineering Standards (Constitution, "Configuration"): BaseSettings
subclasses with the GRAMMQ_ environment prefix; secret-bearing fields
are required SecretStr without defaults; the env-file path comes from
GRAMMQ_ENV_FILE with a ./.env fallback for local runs.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LEASE_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TABLE_PREFIX = "gmq"


class TableNaming(BaseSettings):
    """DB object naming; safe to instantiate at import time (no secrets).

    The prefix is fixed when the first migration is applied: renaming it
    on an existing database is not supported.
    """

    model_config = SettingsConfigDict(
        env_prefix="GRAMMQ_",
        env_file=os.environ.get("GRAMMQ_ENV_FILE", "./.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    table_prefix: str = DEFAULT_TABLE_PREFIX


def table_name(base: str, naming: TableNaming | None = None) -> str:
    """`messages` -> `gmq_messages` (empty prefix disables it)."""
    prefix = (naming or TableNaming()).table_prefix
    return f"{prefix}_{base}" if prefix else base


class TestDatabase(BaseSettings):
    """Optional Postgres URL for the contract suite (Testing standard):
    unset -> DB-gated tests skip.

    Never the deploy env-file (Constitution: real secrets and `.env` are
    never used in tests): the file is `./.env.test` or `GRAMMQ_TEST_ENV_FILE`.
    The plain `TEST_DATABASE_URL` name stays valid for CI.
    """

    model_config = SettingsConfigDict(
        env_file=os.environ.get("GRAMMQ_TEST_ENV_FILE", "./.env.test"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    test_database_url: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GRAMMQ_TEST_DATABASE_URL", "TEST_DATABASE_URL"),
    )


class Settings(BaseSettings):
    """Runtime settings; every field maps to a GRAMMQ_* variable."""

    model_config = SettingsConfigDict(
        env_prefix="GRAMMQ_",
        env_file=os.environ.get("GRAMMQ_ENV_FILE", "./.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr
    broker_lease_seconds: int = DEFAULT_LEASE_SECONDS
    broker_max_attempts: int = DEFAULT_MAX_ATTEMPTS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Fail-fast entry point: a missing GRAMMQ_DATABASE_URL aborts here."""
    # database_url has no default by design (fail-fast on a secret);
    # pydantic-settings injects it from the environment at runtime.
    return Settings()  # type: ignore[call-arg]
