"""Async engine and session factory.

The SecretStr settings value is unwrapped exactly once, here (RQ-10).
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gram_mq.settings import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    engine = create_async_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True,
    )
    return engine


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
