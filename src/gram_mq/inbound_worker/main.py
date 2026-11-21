"""`gram-mq-inbound-worker` console-script entry (spec 007: US2, FR-013).

Assembly point of the runtime adapters: PostgresInboundBroker +
HttpxUpdateDeliverer over the shared settings channel, then the
claim-work loop. This is the only place where the inbound worker domain
meets concrete technologies (Constitution I). Fail-fast on the delivery
URL: the process exists to deliver, an unset target is a deployment
error, not a runtime condition.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from gram_mq.adapters.http.deliverer import HttpxUpdateDeliverer
from gram_mq.adapters.postgres.inbound_broker import PostgresInboundBroker
from gram_mq.inbound_worker.runner import run_inbound_worker
from gram_mq.settings import get_settings

logger = logging.getLogger("gram_mq.inbound_worker")


def _run() -> None:
    settings = get_settings()
    if settings.inbound_delivery_url is None:
        # Fail-fast (Constitution, Configuration): the worker exists to
        # deliver — without a target it would spin on claims with nowhere
        # to send them.
        raise SystemExit(
            "GRAMMQ_INBOUND_DELIVERY_URL is not set; the inbound worker "
            "needs the external update endpoint"
        )
    broker = PostgresInboundBroker(
        database_url=settings.database_url.get_secret_value(),
        lease_seconds=settings.broker_lease_seconds,
        max_attempts=settings.broker_max_attempts,
    )
    auth_value = settings.inbound_delivery_auth_header_value
    deliverer = HttpxUpdateDeliverer(
        url=settings.inbound_delivery_url,
        auth_header_name=settings.inbound_delivery_auth_header_name,
        auth_header_value=(
            auth_value.get_secret_value() if auth_value is not None else None
        ),
        timeout_seconds=settings.inbound_delivery_timeout_seconds,
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    async def body() -> None:
        try:
            await run_inbound_worker(
                broker=broker,
                deliverer=deliverer,
                poll_interval=settings.worker_poll_interval,
                backoff_base=settings.worker_backoff_base_seconds,
                worker_id=worker_id,
                max_attempts=settings.broker_max_attempts,
            )
        finally:
            await deliverer.aclose()
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
        logger.info("inbound worker stopped")
