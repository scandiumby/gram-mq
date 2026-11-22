"""Domain contract of Telegram text delivery (spec 003) and of the
synchronous Bot API proxy (spec 008) — one subject domain, one ports
file (Constitution I).

The domain (worker, api) depends only on these ports; aiogram and httpx
are adapters, test fakes are others. Error taxonomy is part of the
contract: the worker's retry policy is built on `retry_after` and
`permanent`; the proxy's mapping is built on "an answer exists / no
answer arrived".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


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


@dataclass(frozen=True, slots=True)
class TelegramMethodResponse:
    """A raw Bot API answer; the proxy passes it through untouched.

    Exists ⇔ Telegram produced an HTTP response — "no answer" is
    TelegramCallError, never an empty object.
    """

    status: int
    content_type: str
    body: bytes
    #: Seconds from Telegram's 429 Retry-After header (None when absent
    #: or non-numeric); the proxy echoes it back to its own caller.
    retry_after: float | None = None


class TelegramCallError(Exception):
    """No HTTP answer from Telegram: kind is the whole taxonomy.

    Deliberately no free-text reason: nothing a caller could echo into
    logs can carry the bot token (Constitution III).
    """

    def __init__(self, kind: Literal["network", "timeout"]) -> None:
        super().__init__(kind)
        self.kind = kind


@runtime_checkable
class TelegramMethodCaller(Protocol):
    async def call_method(
        self,
        *,
        bot_token: str,
        method: str,
        content_type: str,
        body: bytes,
    ) -> TelegramMethodResponse:
        """Call an arbitrary Bot API method with a raw body.

        `content_type`/`body` travel to Telegram verbatim (JSON or
        multipart); the answer returns as-is. Raises TelegramCallError
        only when no HTTP answer arrived at all.
        """
        ...
