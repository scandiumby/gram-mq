"""FastAPI application: the machine-realm inbound API (spec 003: US2),
the operator bootstrap endpoints (spec 005: US2; spec 006: US1/US3), the
Telegram webhook surface (spec 007: US1), and the synchronous Bot API
proxy (spec 008: US1).

The app depends on ports only (Constitution I): the broker, the key
verifier, the key activator, the bot provisioner/revoker, the inbound
broker, the webhook authenticator, the bot config, and the Telegram
method caller are injected, which is what makes the whole surface
testable on the memory brokers and fakes. Nothing here waits for the
external server (FR-005; spec 007 FR-002); the webhook's only
synchronous work is validation + enqueue, and the proxy's only
synchronous work is the Telegram call itself (spec 008 FR-002).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import SecretStr

from gram_mq.api.auth import parse_key_material
from gram_mq.api.schemas import BotCreateIn, MessageIn
from gram_mq.bot_admin import DuplicateTokenError
from gram_mq.ports.broker import BrokerPort, OutboundMessage, UnknownBotError
from gram_mq.ports.inbound import InboundBrokerPort, InboundUpdate
from gram_mq.ports.telegram import TelegramCallError, TelegramMethodCaller
from gram_mq.settings import DEFAULT_TELEGRAM_PROXY_MAX_BODY_BYTES
from gram_mq.worker.bot_config import BotConfig, NotRegistered, Ready

logger = logging.getLogger("gram_mq.api")


class KeyVerifier(Protocol):
    async def verify(self, key: str | None) -> bool: ...


#: The Telegram webhook surface's verdict: "ok" authenticates, "unknown"
#: maps to 404 (missing bot and disabled reception are indistinguishable
#: by design), "rejected" maps to 401 (spec 007, FR-009).
WebhookCheck = Literal["ok", "unknown", "rejected"]


class WebhookAuth(Protocol):
    async def check(self, bot_slug: str, presented: str | None) -> WebhookCheck: ...


#: Bot API methods that conflict with system-managed flows (spec 008,
#: FR-004): getUpdates fights the webhook receiver (Telegram answers
#: 409 to it under an active webhook anyway), setWebhook/deleteWebhook
#: fight the operator CLI (spec 007), logOut/close switch the bot to
#: polling. Fixed by the contract — the settings whitelist can never
#: re-open any of these.
BANNED_TELEGRAM_METHODS = frozenset(
    {"getUpdates", "setWebhook", "deleteWebhook", "logOut", "close"}
)
_BANNED_METHODS_LOWER = frozenset(name.lower() for name in BANNED_TELEGRAM_METHODS)

#: Bot API method names are ASCII letters (camelCase in the wild); the
#: proxy route validates against this before anything else.
TELEGRAM_METHOD_NAME = re.compile(r"^[A-Za-z]{1,64}$")

MethodPolicy = Literal["ok", "banned", "not_allowed"]


def resolve_method_policy(method: str, allowed: frozenset[str]) -> MethodPolicy:
    """The banned list wins over the allowed one (FR-004 over FR-005);
    matching is case-insensitive — Telegram itself is case-sensitive, so
    blocking every spelling of a banned name is strictly safer."""
    name = method.lower()
    if name in _BANNED_METHODS_LOWER:
        return "banned"
    return "ok" if name in allowed else "not_allowed"


def parse_allowed_methods(csv: str) -> frozenset[str]:
    """Settings CSV -> the lowercase allowed set; garbage or an empty
    list aborts startup (research R9: an explicit list or nothing)."""
    if not csv.strip():
        raise ValueError("the Telegram proxy method list must not be empty")
    names: list[str] = []
    for item in csv.split(","):
        name = item.strip().lower()
        if not TELEGRAM_METHOD_NAME.fullmatch(name):
            raise ValueError(f"invalid Telegram proxy method name: {item.strip()!r}")
        names.append(name)
    return frozenset(names)


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


def _multipart_boundary(content_type: str) -> str | None:
    """The boundary parameter of a multipart/form-data header, if sane."""
    _, _, params = content_type.partition(";")
    for param in params.split(";"):
        name, sep, value = param.partition("=")
        if sep and name.strip().lower() == "boundary":
            return value.strip().strip('"') or None
    return None


def _multipart_well_formed(body: bytes, boundary: str) -> bool:
    """Structural sanity only: the closing delimiter must be there.
    Telegram diagnoses field-level details far better than a proxy can
    (research R6) — and its 400 answers travel back verbatim anyway."""
    closing = f"--{boundary}--".encode()
    return bool(body) and closing in body


def _json_parseable(body: bytes) -> bool:
    try:
        json.loads(body)
    except ValueError:
        return False
    return True


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
    inbound_broker: InboundBrokerPort | None = None,
    webhook_auth: WebhookAuth | None = None,
    bot_config: BotConfig | None = None,
    method_caller: TelegramMethodCaller | None = None,
    allowed_methods: frozenset[str] = frozenset(),
    proxy_max_body_bytes: int = DEFAULT_TELEGRAM_PROXY_MAX_BODY_BYTES,
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

    if inbound_broker is not None and webhook_auth is not None:
        # The Telegram webhook surface (spec 007, US1): registered only
        # when both ports are injected — an outbound-only deploy (no
        # webhook master key) keeps the whole /webhooks/* space a 404.
        @app.post("/webhooks/{bot_slug}", status_code=status.HTTP_200_OK)
        async def receive_webhook_update(
            bot_slug: str,
            payload: Annotated[dict[str, Any], Body()],
            x_telegram_bot_api_secret_token: str | None = Header(
                default=None, alias="X-Telegram-Bot-Api-Secret-Token"
            ),
        ) -> dict[str, str]:
            verdict = await webhook_auth.check(
                bot_slug, x_telegram_bot_api_secret_token
            )
            # Missing header, wrong value — one undifferentiated 401, no
            # secret echo; a missing bot and disabled reception share one
            # 404 — no oracle (FR-009).
            if verdict == "unknown":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="not found"
                )
            if verdict != "ok":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
                )
            update_id = payload.get("update_id")
            if isinstance(update_id, bool) or not isinstance(update_id, int):
                # The only field the system must understand (journal,
                # consumer-side dedup); everything else travels verbatim.
                # Plain 422: the HTTP_422_UNPROCESSABLE_ENTITY constant
                # is deprecated by starlette.
                raise HTTPException(
                    status_code=422, detail="update_id must be an integer"
                )
            update = InboundUpdate(
                id=uuid.uuid4(),
                bot_slug=bot_slug,
                update_id=update_id,
                payload=payload,
            )
            try:
                journaled = await inbound_broker.enqueue_inbound(update)
            except UnknownBotError as exc:
                # The bot vanished between the auth SELECT and the insert.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="not found"
                ) from exc
            except Exception as exc:
                # Storage failure: NOT a 200 — Telegram must retry (FR-002,
                # US1 scenario 4). Boundary catch: logged, never silent.
                logger.error(
                    "webhook enqueue failed bot=%s update_id=%s: %s",
                    bot_slug,
                    update_id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="storage unavailable",
                ) from exc
            # Payload contents are never logged (FR-008); update_id keys
            # every lifecycle event (FR-010).
            if journaled:
                logger.info(
                    "webhook accepted bot=%s update_id=%s id=%s",
                    bot_slug,
                    update_id,
                    update.id,
                )
            else:
                logger.info(
                    "webhook retry absorbed bot=%s update_id=%s", bot_slug, update_id
                )
            return {"status": "ok"}

    if bot_config is not None and method_caller is not None:
        # The synchronous Bot API proxy (spec 008, US1): registered only
        # when both ports are injected — the whole /api/v1/telegram/*
        # space stays a 404 otherwise (the /webhooks/* pattern). A pure
        # pipe: no queue writes, no retries, nothing synchronous beyond
        # the Telegram call itself (FR-002, Constitution II).
        @app.post(
            "/api/v1/telegram/{bot_slug}/{method}",
            dependencies=[Depends(require_api_key)],
        )
        async def call_telegram_method(
            bot_slug: str, method: str, request: Request
        ) -> Response:
            started = time.monotonic()

            def refuse(status_code: int, reason: str, detail: str) -> HTTPException:
                logger.warning(
                    "proxy denied bot=%s method=%s reason=%s dur=%dms",
                    bot_slug,
                    method,
                    reason,
                    int((time.monotonic() - started) * 1000),
                )
                return HTTPException(status_code=status_code, detail=detail)

            if not TELEGRAM_METHOD_NAME.fullmatch(method):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="not found"
                )
            if resolve_method_policy(method, allowed_methods) != "ok":
                # "banned" and "not allowed" share one answer — no oracle
                # for the internal classification (FR-004/FR-005); the
                # banned list wins over any settings extension.
                raise refuse(
                    status.HTTP_403_FORBIDDEN,
                    "method_not_allowed",
                    "method not allowed",
                )
            lookup = await bot_config.token_for(bot_slug)
            if isinstance(lookup, NotRegistered):
                # The same 404 as POST /api/v1/messages: the registry is
                # the single source of truth.
                raise refuse(status.HTTP_404_NOT_FOUND, "not_found", "bot not found")
            if not isinstance(lookup, Ready):
                # No decryptable pair — no calls are made by that bot
                # (Constitution III, spec 006); retryable for the operator.
                raise refuse(
                    status.HTTP_503_SERVICE_UNAVAILABLE, "not_ready", "bot not ready"
                )
            content_type = request.headers.get("Content-Type", "")
            media_base = content_type.split(";", 1)[0].strip().lower()
            is_json = media_base == "application/json" or media_base.endswith("+json")
            is_multipart = media_base == "multipart/form-data"
            if not is_json and not is_multipart:
                raise refuse(
                    415, "unsupported_content_type", "unsupported content type"
                )
            declared_length = request.headers.get("Content-Length")
            if (
                declared_length is not None
                and declared_length.isdigit()
                and int(declared_length) > proxy_max_body_bytes
            ):
                # Refused before the body is read (research R6).
                raise refuse(413, "request_body_too_large", "request body too large")
            body = await request.body()
            if len(body) > proxy_max_body_bytes:
                # Chunked requests can outgrow the header's word.
                raise refuse(413, "request_body_too_large", "request body too large")
            if is_multipart:
                boundary = _multipart_boundary(content_type)
                if boundary is None or not _multipart_well_formed(body, boundary):
                    raise refuse(422, "invalid_request_body", "invalid request body")
            elif not _json_parseable(body):
                raise refuse(422, "invalid_request_body", "invalid request body")
            # Bodies are never logged, the token never leaves this frame
            # (Constitution III, FR-009).
            try:
                result = await method_caller.call_method(
                    bot_token=lookup.token,
                    method=method,
                    content_type=content_type,
                    body=body,
                )
            except TelegramCallError as exc:
                logger.warning(
                    "proxy transport bot=%s method=%s kind=%s dur=%dms",
                    bot_slug,
                    method,
                    exc.kind,
                    int((time.monotonic() - started) * 1000),
                )
                if exc.kind == "timeout":
                    raise HTTPException(
                        status_code=504, detail="telegram timeout"
                    ) from exc
                raise HTTPException(
                    status_code=502, detail="telegram unreachable"
                ) from exc
            # Any HTTP answer keeps Telegram's own status and body; the
            # single remap is 401 — ours means "wrong X-API-Key", so a
            # token rejection (revoked at BotFather) becomes 503 while
            # the body still shows error_code 401 (research R5).
            final_status = 503 if result.status == 401 else result.status
            headers = (
                {"Retry-After": str(int(result.retry_after))}
                if result.status == 429 and result.retry_after is not None
                else None
            )
            logger.info(
                "proxy bot=%s method=%s status=%s dur=%dms",
                bot_slug,
                method,
                final_status,
                int((time.monotonic() - started) * 1000),
            )
            return Response(
                content=result.body,
                status_code=final_status,
                media_type=result.content_type,
                headers=headers,
            )

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
