"""httpx adapter for TelegramMethodCaller (spec 008, Constitution I).

One shared ``httpx.AsyncClient`` per process POSTs the caller's raw body
to ``api.telegram.org/bot<token>/<method>`` without re-encoding (JSON
and multipart alike). The token lives only in the URL, so every httpx
exception text is redacted before the cause chain can surface anywhere
(Constitution III — same discipline as ``classify(secret=…)`` in the
aiogram sender).
"""

from __future__ import annotations

import httpx

from gram_mq.ports.telegram import TelegramCallError, TelegramMethodResponse

TELEGRAM_API_BASE = "https://api.telegram.org"


def _redact(exc: BaseException, token: str) -> BaseException:
    """Strip the token from an exception's args in place.

    httpx embeds the request URL (hence the token) into connect and
    timeout texts; the ``from`` cause must not become a log leak.
    """

    def clean(arg: object) -> object:
        return arg.replace(token, "<redacted>") if isinstance(arg, str) else arg

    exc.args = tuple(clean(arg) for arg in exc.args)
    return exc


class HttpxTelegramMethodCaller:
    """TelegramMethodCaller over a shared ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
            )
        )
        self._owns_client = client is None

    async def call_method(
        self,
        *,
        bot_token: str,
        method: str,
        content_type: str,
        body: bytes,
    ) -> TelegramMethodResponse:
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
        try:
            response = await self._client.post(
                url, content=body, headers={"Content-Type": content_type}
            )
        except httpx.TimeoutException as exc:
            raise TelegramCallError("timeout") from _redact(exc, bot_token)
        except (httpx.HTTPError, OSError) as exc:
            raise TelegramCallError("network") from _redact(exc, bot_token)
        retry_after_header = response.headers.get("Retry-After")
        retry_after: float | None = None
        if retry_after_header is not None:
            try:
                seconds = int(retry_after_header.strip())
            except ValueError:
                seconds = 0
            retry_after = float(seconds) if seconds > 0 else None
        return TelegramMethodResponse(
            status=response.status_code,
            content_type=response.headers.get("Content-Type", "application/json"),
            body=response.content,
            retry_after=retry_after,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
