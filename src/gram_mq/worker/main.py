"""`gram-mq-worker` console-script entry (spec 003: US3, FR-013).

Assembly point of the runtime adapters: PostgresBroker +
AiogramTelegramSender + FileBotConfig over the shared settings channel,
then the claim-work loop. This is the only place where the worker domain
meets concrete technologies (Constitution I).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path

from gram_mq.adapters.postgres.broker import PostgresBroker
from gram_mq.adapters.telegram.aiogram_sender import AiogramTelegramSender
from gram_mq.settings import get_settings
from gram_mq.worker.bot_config import FileBotConfig
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
    bot_config = FileBotConfig(Path(settings.bots_dir))
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
