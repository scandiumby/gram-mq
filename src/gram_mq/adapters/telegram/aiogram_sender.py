"""aiogram 3 adapter for TelegramSender (Constitution I / Stack: Bot only).

One `aiogram.Bot` per token (cached) reuses the underlying aiohttp session;
exception mapping lives in the pure `classify` function so the taxonomy is
testable without network.
"""

from __future__ import annotations

from asyncio import TimeoutError as AsyncTimeoutError

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

from gram_mq.ports.telegram import TelegramSendError

_bot_cache: dict[str, Bot] = {}


def classify(exc: BaseException) -> TelegramSendError:
    """Map an aiogram/asyncio exception to the port's error taxonomy."""
    if isinstance(exc, TelegramRetryAfter):
        return TelegramSendError(
            "telegram rate limit", retry_after=float(exc.retry_after)
        )
    if isinstance(exc, (TelegramNetworkError, AsyncTimeoutError, OSError)):
        return TelegramSendError("telegram network error")
    if isinstance(
        exc, (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError)
    ):
        return TelegramSendError(
            f"telegram rejected the message: {type(exc).__name__}",
            permanent=True,
        )
    return TelegramSendError(
        f"telegram send failed: {type(exc).__name__}", permanent=True
    )


class AiogramTelegramSender:
    """TelegramSender over aiogram 3 `Bot.send_message` (plain text)."""

    async def send_text(self, *, bot_token: str, chat_id: str, text: str) -> str:
        bot = _bot_cache.get(bot_token)
        if bot is None:
            bot = Bot(token=bot_token)
            _bot_cache[bot_token] = bot
        try:
            message = await bot.send_message(chat_id=chat_id, text=text)
        except TelegramRetryAfter as exc:
            raise classify(exc) from exc
        except TelegramNetworkError as exc:
            raise classify(exc) from exc
        except (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramUnauthorizedError,
        ) as exc:
            raise classify(exc) from exc
        except TimeoutError as exc:  # pragma: no cover - defensive
            raise classify(exc) from exc
        return str(message.message_id)

    async def aclose(self) -> None:
        """Close every cached bot session (worker shutdown)."""
        for bot in _bot_cache.values():
            await bot.session.close()
        _bot_cache.clear()
