"""Domain contract of Telegram text delivery (spec 003, Constitution I).

The domain (worker) depends only on this port; aiogram is one adapter, the
test fake is another. Error taxonomy is part of the contract: the worker's
retry policy is built on `retry_after` and `permanent`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TelegramSendError(Exception):
    """A failed send attempt; `reason` never contains the bot token."""

    def __init__(
        self,
        reason: str,
        *,
        retry_after: float | None = None,
        permanent: bool = False,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after
        self.permanent = permanent


@runtime_checkable
class TelegramSender(Protocol):
    async def send_text(self, *, bot_token: str, chat_id: str, text: str) -> str:
        """Send plain text; return the Telegram message id.

        Raises TelegramSendError on any failure. Plain text only in v1
        (no parse_mode); never returns a partial result.
        """
        ...
