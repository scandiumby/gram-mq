"""Domain contract of update delivery to the external bot server
(spec 007, Constitution I).

The domain (inbound worker) depends only on this port; the httpx adapter
is one implementation, the test fake is another. Error taxonomy is part
of the contract: the worker's retry policy is built on ``retry_after``
and ``permanent`` (mirrors ``ports/telegram.py``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class DeliveryError(Exception):
    """A failed delivery attempt; `reason` never contains secrets."""

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
class UpdateDeliverer(Protocol):
    async def deliver_update(self, *, update: dict[str, Any]) -> None:
        """POST the update payload verbatim to the external endpoint.

        Success is a 2xx. Any failure raises DeliveryError; the payload is
        never modified, interpreted, or logged by the implementation.
        """
        ...
