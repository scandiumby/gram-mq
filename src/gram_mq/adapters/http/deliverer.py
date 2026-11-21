"""httpx adapter for UpdateDeliverer (spec 007, Constitution I).

One persistent ``httpx.AsyncClient`` per worker process posts the update
payload verbatim; the optional auth header rides along only when its
secret value is configured. Outcome mapping lives in the pure
``classify`` function so the taxonomy is testable without network
(mirrors the aiogram sender's ``classify``). The auth header value never
appears in reasons or logs.
"""

from __future__ import annotations

from typing import Any

import httpx

from gram_mq.ports.deliverer import DeliveryError


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a numeric Retry-After header; None when absent/odd
    (HTTP-date forms included — the exponential backoff covers them)."""
    if value is None:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    return float(seconds) if seconds > 0 else None


def classify(status_code: int, retry_after_header: str | None) -> None:
    """Map an external response to the port's error taxonomy.

    2xx — success (returns); 429 — a rate-limit pause (``retry_after``
    when the header parses); timeouts/timeouts-in-status, 5xx —
    retryable; every other non-2xx (3xx, 4xx) — permanent: the endpoint
    rejected the update, a retry is pointless (FR-006).
    """
    if 200 <= status_code < 300:
        return
    if status_code == 429:
        raise DeliveryError(
            "delivery rate limited", retry_after=_parse_retry_after(retry_after_header)
        )
    if status_code >= 500:
        raise DeliveryError(f"delivery server error: {status_code}")
    raise DeliveryError(f"delivery rejected: {status_code}", permanent=True)


class HttpxUpdateDeliverer:
    """UpdateDeliverer over a persistent ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        url: str,
        auth_header_name: str = "Authorization",
        auth_header_value: str | None = None,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._headers: dict[str, str] = {}
        if auth_header_value is not None:
            self._headers[auth_header_name] = auth_header_value
        self._client = (
            client if client is not None else httpx.AsyncClient(timeout=timeout_seconds)
        )
        self._owns_client = client is None

    async def deliver_update(self, *, update: dict[str, Any]) -> None:
        try:
            response = await self._client.post(
                self._url, json=update, headers=self._headers
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise DeliveryError("delivery network error") from exc
        classify(response.status_code, response.headers.get("Retry-After"))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
