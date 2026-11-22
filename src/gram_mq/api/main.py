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
from gram_mq.adapters.postgres.inbound_broker import PostgresInboundBroker
from gram_mq.adapters.postgres.webhook_auth import SqlAlchemyWebhookAuth
from gram_mq.adapters.telegram.httpx_method_caller import HttpxTelegramMethodCaller
from gram_mq.api.app import (
    BotManagement,
    KeyBootstrap,
    ProvisionOutcome,
    create_app,
    parse_allowed_methods,
)
from gram_mq.api.auth import ApiKeyVerifier, KeyRecord
from gram_mq.bot_admin import provision_bot, revoke_bot
from gram_mq.db import get_session_factory
from gram_mq.models.api_keys import ApiKey
from gram_mq.settings import get_settings
from gram_mq.token_store import TokenFormatError, parse_master_key
from gram_mq.webhook_store import parse_webhook_master_key
from gram_mq.worker.bot_config import SplitFileBotConfig

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
    # Webhook surface (spec 007): injected only when the webhook master
    # key is configured — an outbound-only deploy keeps /webhooks/* off.
    webhook_master_key = parse_webhook_master_key(settings.webhook_master_key)
    inbound_broker = None
    webhook_auth = None
    if webhook_master_key is not None:
        inbound_broker = PostgresInboundBroker(
            database_url=settings.database_url.get_secret_value(),
            lease_seconds=settings.broker_lease_seconds,
            max_attempts=settings.broker_max_attempts,
        )
        webhook_auth = SqlAlchemyWebhookAuth(session_factory, webhook_master_key)
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

    # Synchronous Bot API proxy (spec 008): the same split-token store
    # the worker and the CLI read, plus the raw httpx caller; a garbage
    # or empty allowed-methods CSV aborts startup right here (fail-fast,
    # research R9).
    proxy_bot_config = SplitFileBotConfig(
        bots_dir=bots_dir,
        fetch_half=registry.fetch_half,
        master_key=master_key,
    )
    proxy_caller = HttpxTelegramMethodCaller(
        timeout_seconds=settings.telegram_proxy_timeout_seconds
    )
    proxy_allowed = parse_allowed_methods(settings.telegram_proxy_allowed_methods)

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
        inbound_broker=inbound_broker,
        webhook_auth=webhook_auth,
        bot_config=proxy_bot_config,
        method_caller=proxy_caller,
        allowed_methods=proxy_allowed,
        proxy_max_body_bytes=settings.telegram_proxy_max_body_bytes,
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
