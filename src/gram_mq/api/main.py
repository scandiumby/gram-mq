"""`gram-mq-api` console-script entry (spec 003: US2, FR-013).

Assembly point of the runtime adapters: PostgresBroker + Argon2id key
verifier + bot provisioning over the shared settings channel, then
uvicorn. This is the only place where the API domain meets concrete
technologies (Constitution I).
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from uvicorn import run as uvicorn_run

from gram_mq.adapters.postgres.bot_registry import SqlAlchemyBotRegistry
from gram_mq.adapters.postgres.broker import PostgresBroker
from gram_mq.api.app import (
    BotManagement,
    KeyBootstrap,
    ProvisionOutcome,
    create_app,
)
from gram_mq.api.auth import ApiKeyVerifier, KeyRecord
from gram_mq.bot_admin import provision_bot, revoke_bot
from gram_mq.db import get_session_factory
from gram_mq.models.api_keys import ApiKey
from gram_mq.settings import get_settings
from gram_mq.token_store import TokenFormatError, parse_master_key

logger = logging.getLogger("gram_mq.api")


async def db_activate(
    factory: async_sessionmaker[AsyncSession], prefix: str, key_hash: str
) -> bool:
    """The production KeyActivator body (spec 005, FR-008/FR-010).

    The prefix PK settles concurrent activations: exactly one transaction
    commits, the loser sees IntegrityError -> conflict (False).
    """
    try:
        async with factory() as session:
            async with session.begin():
                session.add(ApiKey(prefix=prefix, key_hash=key_hash))
    except IntegrityError:
        return False
    return True


def build_default_app() -> object:
    """Wire the production adapters into create_app (uvicorn factory)."""

    async def fetch(prefix: str) -> KeyRecord | None:
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(ApiKey, prefix)
            if row is None:
                return None
            return row.key_hash, row.revoked_at is not None

    session_factory = get_session_factory()

    async def activate(prefix: str, key_hash: str) -> bool:
        return await db_activate(session_factory, prefix, key_hash)

    settings = get_settings()
    broker = PostgresBroker(
        database_url=settings.database_url.get_secret_value(),
        lease_seconds=settings.broker_lease_seconds,
        max_attempts=settings.broker_max_attempts,
    )
    bootstrap = KeyBootstrap(
        enabled=settings.key_bootstrap_enabled,
        material=settings.key_bootstrap_material,
    )
    bots_dir = Path(settings.bots_dir)
    registry = SqlAlchemyBotRegistry(session_factory)
    master_key = parse_master_key(settings.token_master_key)

    async def provision(name: str, token: str) -> ProvisionOutcome:
        if master_key is None:
            return ProvisionOutcome(status="misconfigured")
        try:
            slug = await provision_bot(
                bots_dir=bots_dir,
                registry=registry,
                master_key=master_key,
                token=token,
                name=name,
            )
        except TokenFormatError:
            return ProvisionOutcome(status="misconfigured")
        return ProvisionOutcome(status="created", bot_slug=slug)

    async def revoke(bot_slug: str) -> bool:
        return await revoke_bot(bots_dir=bots_dir, registry=registry, bot_slug=bot_slug)

    return create_app(
        broker=broker,
        key_verifier=ApiKeyVerifier(fetch),
        key_bootstrap=bootstrap,
        key_activator=activate,
        bot_management=BotManagement(
            enabled=settings.bot_bootstrap_enabled,
            token=settings.bot_bootstrap_token,
        ),
        bot_provisioner=provision,
        bot_revoker=revoke,
    )


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
