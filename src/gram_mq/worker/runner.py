"""The delivery worker loop (spec 003: US3; Constitution II — non-negotiable).

claim → send → atomic ack. Every failure goes back through the broker
contract: retryable errors re-queue with backoff (429 respects
retry_after), permanent Telegram rejections dead-letter immediately, and
retryable errors that exhaust max_attempts dead-letter with the reason.
A crash between send and ack is closed by lease expiry + reclaim — the
duplicate in the chat is the documented price of at-least-once.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from gram_mq.ports.broker import BrokerPort, Delivery
from gram_mq.ports.telegram import TelegramSender, TelegramSendError
from gram_mq.worker.bot_config import BotConfig, NotReady, NotRegistered

logger = logging.getLogger("gram_mq.worker")

BACKOFF_CAP_SECONDS = 300.0


async def _retry_or_fail(
    delivery: Delivery,
    *,
    broker: BrokerPort,
    delay: timedelta,
    reason: str,
    max_attempts: int,
) -> None:
    attempts = int(delivery.context.get("attempts", 0))
    if attempts + 1 >= max_attempts:
        await broker.dead_letter(delivery, reason=f"max attempts exceeded: {reason}")
        logger.warning(
            "failed id=%s bot=%s attempts=%s: %s",
            delivery.id,
            delivery.bot_slug,
            attempts,
            reason,
        )
        return
    await broker.retry(delivery, delay=delay, reason=reason)
    logger.warning(
        "retry id=%s bot=%s in %.0fs: %s",
        delivery.id,
        delivery.bot_slug,
        delay.total_seconds(),
        reason,
    )


async def process_delivery(
    delivery: Delivery,
    *,
    broker: BrokerPort,
    sender: TelegramSender,
    bot_config: BotConfig,
    backoff_base: float,
    max_attempts: int,
) -> None:
    """Handle one claimed delivery end-to-end (send + outcome)."""
    lookup = await bot_config.token_for(delivery.bot_slug)
    if isinstance(lookup, NotRegistered):
        # Revoked or unknown bot (spec 006, US3): permanent by nature —
        # a deleted bot does not "reappear" — so dead-letter immediately
        # instead of burning the retry budget (FR-015).
        await broker.dead_letter(delivery, reason="bot not registered")
        logger.warning(
            "failed id=%s bot=%s: bot not registered",
            delivery.id,
            delivery.bot_slug,
        )
        return
    if isinstance(lookup, NotReady):
        # Registered but unreadable halves (no file, NULL columns, no
        # master key, tamper, generation mismatch): a normal retryable
        # state — the file or the key setting may reappear (FR-012).
        await _retry_or_fail(
            delivery,
            broker=broker,
            delay=timedelta(seconds=backoff_base),
            reason="bot config unreadable (file/keys)",
            max_attempts=max_attempts,
        )
        return
    token = lookup.token

    try:
        telegram_id = await sender.send_text(
            bot_token=token,
            chat_id=delivery.chat_id,
            text=str(delivery.payload.get("text", "")),
        )
    except TelegramSendError as error:
        if error.permanent:
            await broker.dead_letter(delivery, reason=error.reason)
            logger.warning(
                "failed id=%s bot=%s: %s",
                delivery.id,
                delivery.bot_slug,
                error.reason,
            )
            return
        if error.retry_after is not None:
            # Rate-limit pause (429): waiting for retry_after is not a
            # failed attempt of the message — attempts are not consumed
            # and the exhaustion check does not apply.
            await broker.retry(
                delivery,
                delay=timedelta(seconds=max(error.retry_after, 1.0)),
                reason=error.reason,
                count_attempt=False,
            )
            logger.warning(
                "rate-limit pause id=%s bot=%s in %.0fs: %s",
                delivery.id,
                delivery.bot_slug,
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

    delivery.context["telegram_message_id"] = telegram_id
    await broker.ack(delivery)
    logger.info(
        "delivered id=%s bot=%s telegram_message_id=%s",
        delivery.id,
        delivery.bot_slug,
        telegram_id,
    )


async def run_worker(
    *,
    broker: BrokerPort,
    sender: TelegramSender,
    bot_config: BotConfig,
    poll_interval: float,
    backoff_base: float,
    worker_id: str,
    max_attempts: int,
) -> None:
    """Claim-work loop: no sleep while work exists, sleep when idle."""
    logger.info("worker %s started", worker_id)
    while True:
        delivery = await broker.claim(worker_id=worker_id)
        if delivery is None:
            await asyncio.sleep(poll_interval)
            continue
        await process_delivery(
            delivery,
            broker=broker,
            sender=sender,
            bot_config=bot_config,
            backoff_base=backoff_base,
            max_attempts=max_attempts,
        )
