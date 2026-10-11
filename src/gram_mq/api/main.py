"""`gram-mq-api` console-script entry (spec 003: US2, FR-013).

Assembly point of the runtime adapters: PostgresBroker + Argon2id key
verifier over the shared settings channel, then uvicorn. This is the only
place where the API domain meets concrete technologies (Constitution I).
"""

from __future__ import annotations

import logging

from uvicorn import run as uvicorn_run

from gram_mq.adapters.postgres.broker import PostgresBroker
from gram_mq.api.app import create_app
from gram_mq.api.auth import ApiKeyVerifier, KeyRecord
from gram_mq.db import get_session_factory
from gram_mq.models.api_keys import ApiKey
from gram_mq.settings import get_settings

logger = logging.getLogger("gram_mq.api")


def build_default_app() -> object:
    """Wire the production adapters into create_app (uvicorn factory)."""

    async def fetch(prefix: str) -> KeyRecord | None:
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(ApiKey, prefix)
            if row is None:
                return None
            return row.key_hash, row.revoked_at is not None

    settings = get_settings()
    broker = PostgresBroker(
        database_url=settings.database_url.get_secret_value(),
        lease_seconds=settings.broker_lease_seconds,
        max_attempts=settings.broker_max_attempts,
    )
    return create_app(broker=broker, key_verifier=ApiKeyVerifier(fetch))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    uvicorn_run(
        build_default_app,
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
    )
