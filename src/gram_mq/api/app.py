"""FastAPI application: the machine-realm inbound API (spec 003: US2)
plus the operator bootstrap endpoints (spec 005: US2; spec 006: US1/US3).

The app depends on ports only (Constitution I): the broker, the key
verifier, the key activator, and the bot provisioner/revoker are
injected, which is what makes the whole surface testable on the memory
broker and fakes. Nothing here waits for Telegram (FR-005); the only
synchronous work is validation + enqueue.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import SecretStr

from gram_mq.api.auth import parse_key_material
from gram_mq.api.schemas import BotCreateIn, MessageIn
from gram_mq.bot_admin import DuplicateTokenError
from gram_mq.ports.broker import BrokerPort, OutboundMessage, UnknownBotError

logger = logging.getLogger("gram_mq.api")


class KeyVerifier(Protocol):
    async def verify(self, key: str | None) -> bool: ...


#: Persists an activated key; False means the prefix already exists.
KeyActivator = Callable[[str, str], Awaitable[bool]]


@dataclass(frozen=True)
class KeyBootstrap:
    """Startup snapshot of the two-stage bootstrap settings (spec 005).

    `material` carries the pre-computed `prefix:hash` line from
    `gram-mq key prepare` — never the plaintext key.
    """

    enabled: bool
    material: SecretStr | None


@dataclass(frozen=True)
class ProvisionOutcome:
    """What the provisioning port reports back to the endpoint.

    `misconfigured` covers unset/invalid token setting and a missing
    master key in one status: callers must not differentiate the two.
    """

    status: Literal["created", "duplicate", "misconfigured"]
    bot_slug: str | None = None


#: (name, token) -> outcome; the token arrives from the snapshot, never
#: from the request body.
BotProvisioner = Callable[[str, str], Awaitable[ProvisionOutcome]]

#: slug -> True when a live bot was deleted.
BotRevoker = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class BotManagement:
    """Startup snapshot of the bot management window (spec 006)."""

    enabled: bool
    token: SecretStr | None


def create_app(
    *,
    broker: BrokerPort,
    key_verifier: KeyVerifier,
    key_bootstrap: KeyBootstrap | None = None,
    key_activator: KeyActivator | None = None,
    bot_management: BotManagement | None = None,
    bot_provisioner: BotProvisioner | None = None,
    bot_revoker: BotRevoker | None = None,
) -> FastAPI:
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

    @app.post(
        "/bootstrap/api-key",
        status_code=status.HTTP_201_CREATED,
    )
    async def bootstrap_api_key() -> dict[str, str]:
        # Operator surface outside both realms (spec 005: FR-013 — the
        # first key cannot authenticate itself). Disabled by default:
        # the route behaves as nonexistent (FR-007); the material comes
        # from settings only, never from the request (FR-014).
        if key_bootstrap is None or not key_bootstrap.enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        material = (
            key_bootstrap.material.get_secret_value()
            if key_bootstrap.material is not None
            else None
        )
        parsed = parse_key_material(material) if material is not None else None
        if parsed is None or key_activator is None:
            # One message for unset and corrupt alike — the material is
            # secret and is never echoed in responses or logs (FR-012).
            logger.error("key bootstrap enabled but material missing or invalid")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="bootstrap material missing or invalid",
            )
        prefix, key_hash = parsed
        if not await key_activator(prefix, key_hash):
            logger.warning("api key bootstrap conflict prefix=%s", prefix)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="api key already activated",
            )
        logger.info("api key bootstrap activated prefix=%s", prefix)
        return {"status": "activated", "prefix": prefix}

    @app.post("/bootstrap/bots", status_code=status.HTTP_201_CREATED)
    async def bootstrap_bots(bot_in: BotCreateIn) -> dict[str, str]:
        # Operator surface outside both realms (spec 006: US1): disabled
        # by default, the full token comes from the snapshot setting only
        # — the request body carries just the public bot name.
        if bot_management is None or not bot_management.enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        token = (
            bot_management.token.get_secret_value()
            if bot_management.token is not None
            else None
        )
        if token is None or bot_provisioner is None:
            logger.error("bot bootstrap enabled but token missing or invalid")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="bootstrap token or master key missing or invalid",
            )
        try:
            outcome = await bot_provisioner(bot_in.name, token)
        except DuplicateTokenError:
            logger.warning("bot bootstrap conflict by token id")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="bot token already registered",
            ) from None
        except OSError as error:
            logger.error("bot bootstrap write failure: %s", type(error).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="provisioning failed; retry",
            ) from None
        if outcome.status == "duplicate":
            logger.warning("bot bootstrap conflict by token id")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="bot token already registered",
            )
        if outcome.status == "misconfigured":
            # One message for an unset and a corrupt setting/master key
            # alike (FR-011): the material is secret, never echoed.
            logger.error("bot bootstrap enabled but settings unusable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="bootstrap token or master key missing or invalid",
            )
        logger.info("bot bootstrap created bot_slug=%s", outcome.bot_slug)
        return {"status": "created", "bot_slug": str(outcome.bot_slug)}

    @app.delete("/bootstrap/bots/{bot_slug}")
    async def bootstrap_bots_revoke(bot_slug: str) -> dict[str, str]:
        if bot_management is None or not bot_management.enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        if bot_revoker is None or not await bot_revoker(bot_slug):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="not found"
            )
        logger.info("bot bootstrap revoked bot_slug=%s", bot_slug)
        return {"status": "revoked", "bot_slug": bot_slug}

    return app
