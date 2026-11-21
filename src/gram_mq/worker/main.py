"""`gram-mq-worker` console-script entry (spec 003: US3, FR-013).

Assembly point of the runtime adapters: PostgresBroker +
AiogramTelegramSender + SplitFileBotConfig over the shared settings
channel, then the claim-work loop. This is the only place where the
worker domain meets concrete technologies (Constitution I).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path

from gram_mq.adapters.postgres.bot_registry import SqlAlchemyBotRegistry
from gram_mq.adapters.postgres.broker import PostgresBroker
from gram_mq.adapters.telegram.aiogram_sender import AiogramTelegramSender
from gram_mq.db import get_session_factory
from gram_mq.settings import get_settings
from gram_mq.token_store import parse_master_key
from gram_mq.worker.bot_config import SplitFileBotConfig
from gram_mq.worker.runner import run_worker

logger = logging.getLogger("gram_mq.worker")


def _run() -> None:
    settings = get_settings()
    broker = PostgresBroker(
        database_url=settings.database_url.get_secret_value(),
        lease_seconds=settings.broker_lease_seconds,
        max_attempts=settings.broker_max_attempts,
    )
    sender = AiogramTelegramSender()
    registry = SqlAlchemyBotRegistry(get_session_factory())
    bot_config = SplitFileBotConfig(
        bots_dir=Path(settings.bots_dir),
        fetch_half=registry.fetch_half,
        master_key=parse_master_key(settings.token_master_key),
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    async def body() -> None:
        try:
            await run_worker(
                broker=broker,
                sender=sender,
                bot_config=bot_config,
                poll_interval=settings.worker_poll_interval,
                backoff_base=settings.worker_backoff_base_seconds,
                worker_id=worker_id,
                max_attempts=settings.broker_max_attempts,
            )
        finally:
            await sender.aclose()
            await broker.aclose()

    asyncio.run(body())


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        _run()
    except KeyboardInterrupt:
        logger.info("worker stopped")
