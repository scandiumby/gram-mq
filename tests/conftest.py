"""Contract-suite fixtures: one suite, two adapters (FR-010).

- memory: always, no infrastructure, injectable clock (RQ-8);
- postgres: only when the test database URL is configured (env var
  `GRAMMQ_TEST_DATABASE_URL`/`TEST_DATABASE_URL` or `./.env.test` via
  TestDatabase settings), otherwise the parameter is skipped
  (Constitution, Engineering Standards, Testing).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from gram_mq import db, settings  # noqa: F401
from gram_mq.adapters.memory.broker import InMemoryBroker, MemoryStore

# Import-time coverage: models and settings execute their declarations on
# import in every entry point (api, worker, alembic); the contract suite
# exercises the same import path for the memory adapter.
from gram_mq.models import base, bots, claim_state, messages  # noqa: F401
from gram_mq.ports.broker import BrokerPort
from gram_mq.settings import TestDatabase, table_name

_test_database = TestDatabase()
TEST_DATABASE_URL = (
    _test_database.test_database_url.get_secret_value()
    if _test_database.test_database_url
    else ""
)
REPO_ROOT = Path(__file__).resolve().parents[1]


class SeededBroker(BrokerPort, Protocol):
    """BrokerPort plus the adapter-side seeding helper used by tests."""

    async def register_bot(self, bot_slug: str) -> None: ...


@dataclass
class FakeClock:
    """Deterministic time source; memory-broker tests jump it forward."""

    now_value: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )

    def now(self) -> datetime:
        return self.now_value

    def advance(self, delta: timedelta) -> None:
        self.now_value += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=["memory", "postgres"])
def adapter(request: pytest.FixtureRequest) -> str:
    if request.param == "postgres" and not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    return str(request.param)


def _run_migrations(url: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "GRAMMQ_DATABASE_URL": url},
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def _pg_schema() -> None:
    if TEST_DATABASE_URL:
        _run_migrations(TEST_DATABASE_URL)


@pytest_asyncio.fixture
async def make_broker(
    adapter: str, clock: FakeClock, _pg_schema: None
) -> AsyncIterator[Callable[..., SeededBroker]]:
    """Factory: brokers created in one test share the store (journal!);
    isolation is between tests, not within one.
    """
    store = MemoryStore()
    brokers: list[SeededBroker] = []

    if adapter == "postgres":
        engine = create_async_engine(TEST_DATABASE_URL)
        tables = ", ".join(
            table_name(name) for name in ("messages", "bot_claim_state", "bots")
        )
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
        await engine.dispose()

    def _make(*, lease_seconds: float = 60.0, max_attempts: int = 5) -> SeededBroker:
        if adapter == "memory":
            broker: SeededBroker = InMemoryBroker(
                store,
                now=clock.now,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )
        else:
            from gram_mq.adapters.postgres.broker import PostgresBroker

            broker = PostgresBroker(
                database_url=TEST_DATABASE_URL,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )
        brokers.append(broker)
        return broker

    yield _make

    for broker in brokers:
        close = getattr(broker, "aclose", None)
        if close is not None:
            await close()


@pytest_asyncio.fixture
async def broker(make_broker: Callable[..., SeededBroker]) -> SeededBroker:
    return make_broker()
