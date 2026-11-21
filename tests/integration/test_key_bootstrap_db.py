"""DB-gated integration of the two-stage key bootstrap (spec 005: US3).

Runs only against the test database (Constitution, Testing):
GRAMMQ_TEST_DATABASE_URL / TEST_DATABASE_URL or ./.env.test — otherwise
the whole module is skipped. Never the deploy env-file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gram_mq.api.auth import ApiKeyVerifier, format_key_material, parse_key_material
from gram_mq.api.main import db_activate
from gram_mq.settings import table_name
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"
)

CHEAP_HASHER = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)
API_KEY = "gmq_" + "b" * 43


@pytest_asyncio.fixture
async def factory(
    _pg_schema: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {table_name('api_keys')} CASCADE"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_db_activate_inserts_row_and_rejects_repeat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    digest = CHEAP_HASHER.hash(API_KEY)
    material = format_key_material(API_KEY[:12], digest)
    parsed = parse_key_material(material)
    assert parsed is not None
    prefix, key_hash = parsed
    assert (prefix, key_hash) == (API_KEY[:12], digest)

    assert await db_activate(factory, prefix, key_hash) is True
    # The repeat hits the live prefix PK: IntegrityError -> False.
    assert await db_activate(factory, prefix, key_hash) is False

    async with factory() as session:
        rows = await session.execute(
            text(
                f"SELECT prefix, key_hash FROM {table_name('api_keys')} "
                f"WHERE prefix = '{prefix}'"
            )
        )
        assert rows.all() == [(prefix, digest)], "exactly one row, no key"


async def test_activated_key_verifies_over_the_database(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    digest = CHEAP_HASHER.hash(API_KEY)
    prefix = API_KEY[:12]
    assert await db_activate(factory, prefix, digest) is True

    async def fetch(p: str) -> tuple[str, bool] | None:
        async with factory() as session:
            result = await session.execute(
                text(
                    f"SELECT key_hash, revoked_at FROM {table_name('api_keys')} "
                    f"WHERE prefix = '{p}'"
                )
            )
            row = result.first()
            if row is None:
                return None
            return row[0], row[1] is not None

    verifier = ApiKeyVerifier(fetch, CHEAP_HASHER)
    # US2 scenario 3 in the real environment: the stage-1 plaintext
    # authenticates immediately after activation (FR-011).
    assert await verifier.verify(API_KEY) is True
    assert await verifier.verify("gmq_" + "x" * 43) is False
