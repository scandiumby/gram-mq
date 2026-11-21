"""The inbound delivery worker loop (spec 007: US2; Constitution II).

claim → deliver → atomic ack. Every failure goes back through the broker
contract: retryable errors (timeout, transport, 5xx) re-queue with
exponential backoff, 429 waits out retry_after without spending an
attempt, permanent rejections (other 4xx) dead-letter immediately, and
retryable errors that exhaust max_attempts dead-letter with the reason —
the mirror of worker/runner.py. A crash between the external POST and
ack is closed by lease expiry + reclaim: the duplicate delivery is the
documented price of at-least-once (SC-005).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from gram_mq.ports.deliverer import DeliveryError, UpdateDeliverer
from gram_mq.ports.inbound import InboundBrokerPort, InboundDelivery

logger = logging.getLogger("gram_mq.inbound_worker")

BACKOFF_CAP_SECONDS = 300.0


async def _retry_or_fail(
    delivery: InboundDelivery,
    *,
    broker: InboundBrokerPort,
    delay: timedelta,
    reason: str,
    max_attempts: int,
) -> None:
    attempts = int(delivery.context.get("attempts", 0))
    if attempts + 1 >= max_attempts:
        await broker.dead_letter(delivery, reason=f"max attempts exceeded: {reason}")
        logger.warning(
            "failed id=%s bot=%s update_id=%s attempts=%s: %s",
            delivery.id,
            delivery.bot_slug,
            delivery.update_id,
            attempts,
            reason,
        )
        return
    await broker.retry(delivery, delay=delay, reason=reason)
    logger.warning(
        "retry id=%s bot=%s update_id=%s in %.0fs: %s",
        delivery.id,
        delivery.bot_slug,
        delivery.update_id,
        delay.total_seconds(),
        reason,
    )


async def process_update(
    delivery: InboundDelivery,
    *,
    broker: InboundBrokerPort,
    deliverer: UpdateDeliverer,
    backoff_base: float,
    max_attempts: int,
) -> None:
    """Handle one claimed inbound delivery end-to-end (deliver + outcome)."""
    try:
        await deliverer.deliver_update(update=delivery.payload)
    except DeliveryError as error:
        if error.permanent:
            await broker.dead_letter(delivery, reason=error.reason)
            logger.warning(
                "failed id=%s bot=%s update_id=%s: %s",
                delivery.id,
                delivery.bot_slug,
                delivery.update_id,
                error.reason,
            )
            return
        if error.retry_after is not None:
            # Rate-limit pause (429): waiting for retry_after is not a
            # failed attempt of the update — attempts are not consumed
            # and the exhaustion check does not apply.
            await broker.retry(
                delivery,
                delay=timedelta(seconds=max(error.retry_after, 1.0)),
                reason=error.reason,
                count_attempt=False,
            )
            logger.warning(
                "rate-limit pause id=%s bot=%s update_id=%s in %.0fs: %s",
                delivery.id,
                delivery.bot_slug,
                delivery.update_id,
                error.retry_after,
                error.reason,
            )
            return
        attempts = int(delivery.context.get("attempts", 0))
        delay_seconds = min(backoff_base * 2**attempts, BACKOFF_CAP_SECONDS)
        await _retry_or_fail(
            delivery,
            broker=broker,
            delay=timedelta(seconds=delay_seconds),
            reason=error.reason,
            max_attempts=max_attempts,
        )
        return

    await broker.ack(delivery)
    logger.info(
        "delivered id=%s bot=%s update_id=%s",
        delivery.id,
        delivery.bot_slug,
        delivery.update_id,
    )


async def run_inbound_worker(
    *,
    broker: InboundBrokerPort,
    deliverer: UpdateDeliverer,
    poll_interval: float,
    backoff_base: float,
    worker_id: str,
    max_attempts: int,
) -> None:
    """Claim-work loop: no sleep while work exists, sleep when idle."""
    logger.info("inbound worker %s started", worker_id)
    while True:
        delivery = await broker.claim(worker_id=worker_id)
        if delivery is None:
            await asyncio.sleep(poll_interval)
            continue
        await process_update(
            delivery,
            broker=broker,
            deliverer=deliverer,
            backoff_base=backoff_base,
            max_attempts=max_attempts,
        )
