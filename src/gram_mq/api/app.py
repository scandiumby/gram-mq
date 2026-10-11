"""FastAPI application: the machine-realm inbound API (spec 003: US2).

The app depends on ports only (Constitution I): the broker and the key
verifier are injected, which is what makes the whole surface testable on
the memory broker and a fake verifier. Nothing here waits for Telegram
(FR-005); the only synchronous work is validation + enqueue.
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, status

from gram_mq.api.schemas import MessageIn
from gram_mq.ports.broker import BrokerPort, OutboundMessage, UnknownBotError

logger = logging.getLogger("gram_mq.api")


class KeyVerifier(Protocol):
    async def verify(self, key: str | None) -> bool: ...


def create_app(*, broker: BrokerPort, key_verifier: KeyVerifier) -> FastAPI:
    app = FastAPI(title="gram-mq", version="0.1.0")

    async def require_api_key(
        x_api_key: str | None = Header(default=None),
    ) -> None:
        # Only the header authorizes (FR-002); one undifferentiated 401 for
        # missing, wrong and revoked keys — no reason details, no key echo.
        if not await key_verifier.verify(x_api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
            )

    @app.post(
        "/api/v1/messages",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_api_key)],
    )
    async def post_message(message_in: MessageIn) -> dict[str, str]:
        message = OutboundMessage(
            id=uuid.uuid4(),
            bot_slug=message_in.bot_slug,
            chat_id=message_in.chat_id,
            payload={"text": message_in.text},
        )
        try:
            await broker.enqueue(message)
        except UnknownBotError as exc:
            # The registry is the single source of truth (research R1): the
            # broker's referential-integrity failure is the 404.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="bot not found"
            ) from exc
        logger.info(
            "accepted id=%s bot=%s chat=%s",
            message.id,
            message_in.bot_slug,
            message_in.chat_id,
        )
        return {"id": str(message.id)}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
