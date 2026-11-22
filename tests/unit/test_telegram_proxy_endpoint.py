"""Telegram Bot API proxy endpoint unit contract (spec 008: US1–US4).

Fake BotConfig + fake TelegramMethodCaller over ASGITransport: the whole
proxy surface without infrastructure. Invariants: the check order
(key -> method format -> policy -> token -> content type -> size ->
body -> call), passthrough of Telegram's own statuses/bodies with the
single 401 remap, the default-deny method policy with the banned list
winning over any settings extension, the body limit, one log line per
call — and no token, key, or body ever reaching a response or a log
record (Constitution III, spec FR-008/FR-009).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from gram_mq.api.app import (
    BANNED_TELEGRAM_METHODS,
    create_app,
    parse_allowed_methods,
    resolve_method_policy,
)
from gram_mq.ports.broker import Delivery, OutboundMessage
from gram_mq.ports.telegram import TelegramCallError, TelegramMethodResponse
from gram_mq.settings import DEFAULT_TELEGRAM_PROXY_ALLOWED_METHODS
from gram_mq.worker.bot_config import NotReady, NotRegistered, Ready

SLUG = "0010-arrakis-melange"
TOKEN = "6789012345:AAE-very-secret-token-value"
API_KEY = "gmq_test_key"
DEFAULT_ALLOWED = parse_allowed_methods(DEFAULT_TELEGRAM_PROXY_ALLOWED_METHODS)
JSON_CT = "application/json"


class FakeBotConfig:
    """Per-slug TokenLookup table driven by the test."""

    def __init__(
        self,
        ready: dict[str, str] | None = None,
        not_ready: frozenset[str] = frozenset(),
    ) -> None:
        self._ready = ready if ready is not None else {SLUG: TOKEN}
        self._not_ready = not_ready

    async def token_for(self, bot_slug: str) -> object:
        if bot_slug in self._ready:
            return Ready(self._ready[bot_slug])
        if bot_slug in self._not_ready:
            return NotReady()
        return NotRegistered()


class FakeMethodCaller:
    """Programmed answer (or failure) + the call recorder."""

    def __init__(
        self,
        result: TelegramMethodResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or TelegramMethodResponse(
            status=200, content_type=JSON_CT, body=b'{"ok":true,"result":{"id":42}}'
        )
        self.error = error
        self.calls: list[tuple[str, str, str, bytes]] = []

    async def call_method(
        self, *, bot_token: str, method: str, content_type: str, body: bytes
    ) -> TelegramMethodResponse:
        self.calls.append((bot_token, method, content_type, body))
        if self.error is not None:
            raise self.error
        return self.result


class _OutboundStub:
    """A no-op BrokerPort: this suite never touches the outbound side."""

    async def enqueue(self, message: OutboundMessage) -> None:
        return None

    async def claim(self, *, worker_id: str) -> Delivery | None:
        return None

    async def ack(self, delivery: Delivery) -> None:
        return None

    async def retry(
        self,
        delivery: Delivery,
        delay: object,
        reason: str,
        *,
        count_attempt: bool = True,
    ) -> None:
        return None

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        return None

    async def queue_depth(self, bot_slug: str) -> int:
        return 0


class _KeyStub:
    def __init__(self, valid: str | None = API_KEY) -> None:
        self._valid = valid

    async def verify(self, key: str | None) -> bool:
        return key == self._valid


def make_client(
    bot_config: object,
    caller: FakeMethodCaller,
    *,
    allowed: frozenset[str] = DEFAULT_ALLOWED,
    key: str | None = API_KEY,
    max_body_bytes: int = 52_428_800,
) -> AsyncClient:
    # The verifier always accepts the one real key; the `key` argument
    # only shapes the client's header (the 401 path is "anything else").
    app = create_app(
        broker=_OutboundStub(),
        key_verifier=_KeyStub(API_KEY),
        bot_config=bot_config,  # type: ignore[arg-type]
        method_caller=caller,
        allowed_methods=allowed,
        proxy_max_body_bytes=max_body_bytes,
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": key} if key else {},
    )


def proxy_url(slug: str = SLUG, method: str = "getMe") -> str:
    return f"/api/v1/telegram/{slug}/{method}"


class TestSurfaceControl:
    async def test_routes_absent_when_ports_not_injected(self) -> None:
        app = create_app(broker=_OutboundStub(), key_verifier=_KeyStub())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 404


class TestAuth:
    async def test_wrong_key_401_before_any_port_call(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller, key="wrong") as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 401
        assert response.json() == {"detail": "unauthorized"}
        assert caller.calls == []


class TestMethodFormat:
    async def test_non_letter_method_404(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(method="get-Me"), json={})
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}
        assert caller.calls == []

    async def test_escaped_slash_method_404(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(method="x%2F..%2Fy"), json={})
        assert response.status_code == 404
        assert caller.calls == []


class TestPolicy:
    async def test_every_banned_method_403_without_call(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            for method in sorted(BANNED_TELEGRAM_METHODS):
                response = await client.post(proxy_url(method=method), json={})
                assert response.status_code == 403, method
        assert caller.calls == []

    async def test_banned_case_insensitive(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(method="getupdates"), json={})
        assert response.status_code == 403
        assert caller.calls == []

    async def test_out_of_whitelist_403(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(method="sendPhoto"), json={})
        assert response.status_code == 403
        assert response.json() == {"detail": "method not allowed"}
        assert caller.calls == []

    async def test_whitelist_opens_but_banned_stays_closed(self) -> None:
        caller = FakeMethodCaller()
        allowed = DEFAULT_ALLOWED | parse_allowed_methods("sendPhoto,getUpdates")
        async with make_client(FakeBotConfig(), caller, allowed=allowed) as client:
            opened = await client.post(proxy_url(method="sendPhoto"), json={})
            banned = await client.post(proxy_url(method="getUpdates"), json={})
        assert opened.status_code == 200
        assert banned.status_code == 403
        assert [call[1] for call in caller.calls] == ["sendPhoto"]


class TestToken:
    async def test_unknown_bot_404(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(slug="ghost"), json={})
        assert response.status_code == 404
        assert response.json() == {"detail": "bot not found"}
        assert caller.calls == []

    async def test_not_ready_bot_503_without_call(self) -> None:
        caller = FakeMethodCaller()
        config = FakeBotConfig(ready={}, not_ready=frozenset({SLUG}))
        async with make_client(config, caller) as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 503
        assert response.json() == {"detail": "bot not ready"}
        assert caller.calls == []


class TestBodyValidation:
    async def test_plain_text_content_type_415(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(
                proxy_url(), content=b"hello", headers={"Content-Type": "text/plain"}
            )
        assert response.status_code == 415
        assert response.json() == {"detail": "unsupported content type"}
        assert caller.calls == []

    async def test_broken_json_422(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(
                proxy_url(), content=b"{", headers={"Content-Type": JSON_CT}
            )
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid request body"}
        assert caller.calls == []

    async def test_empty_json_body_422(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(
                proxy_url(), content=b"", headers={"Content-Type": JSON_CT}
            )
        assert response.status_code == 422
        assert caller.calls == []

    async def test_multipart_without_closing_boundary_422(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(
                proxy_url(method="editMessageText"),
                content=b"--abc123\r\ncontent without terminator",
                headers={"Content-Type": "multipart/form-data; boundary=abc123"},
            )
        assert response.status_code == 422
        assert caller.calls == []

    async def test_multipart_without_boundary_parameter_422(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(
                proxy_url(method="editMessageText"),
                content=b"anything",
                headers={"Content-Type": "multipart/form-data"},
            )
        assert response.status_code == 422
        assert caller.calls == []

    async def test_multipart_passes_verbatim(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(
                proxy_url(method="editMessageText"), files={"x": ("f.png", b"PNG")}
            )
        assert response.status_code == 200
        token, method, content_type, body = caller.calls[0]
        assert token == TOKEN
        assert method == "editMessageText"
        assert content_type.startswith("multipart/form-data; boundary=")
        assert b"PNG" in body
        assert body.rstrip().endswith(b"--")


class TestPassthrough:
    async def test_success_body_and_content_type_verbatim(self) -> None:
        body = 'текст ответа с "кавычками"'.encode()
        caller = FakeMethodCaller(
            result=TelegramMethodResponse(
                status=200, content_type="application/json", body=body
            )
        )
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 200
        assert response.content == body
        assert response.headers["Content-Type"].startswith("application/json")

    async def test_call_receives_slug_token_and_body(self) -> None:
        caller = FakeMethodCaller()
        payload = {"chat_id": "42", "text": "edited"}
        async with make_client(FakeBotConfig(), caller) as client:
            await client.post(proxy_url(method="editMessageText"), json=payload)
        token, method, content_type, body = caller.calls[0]
        assert token == TOKEN
        assert method == "editMessageText"
        assert content_type.startswith(JSON_CT)
        assert b"edited" in body

    async def test_telegram_error_keeps_status_and_body(self) -> None:
        body = (
            b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'
        )
        caller = FakeMethodCaller(
            result=TelegramMethodResponse(status=400, content_type=JSON_CT, body=body)
        )
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 400
        assert response.content == body

    async def test_telegram_429_echoes_retry_after_header(self) -> None:
        caller = FakeMethodCaller(
            result=TelegramMethodResponse(
                status=429, content_type=JSON_CT, body=b"{}", retry_after=9.0
            )
        )
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "9"
        assert response.content == b"{}"

    async def test_telegram_401_remaps_to_503_body_kept(self) -> None:
        body = b'{"ok":false,"error_code":401,"description":"Unauthorized"}'
        caller = FakeMethodCaller(
            result=TelegramMethodResponse(status=401, content_type=JSON_CT, body=body)
        )
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 503
        assert response.content == body

    async def test_timeout_maps_to_504(self) -> None:
        caller = FakeMethodCaller(error=TelegramCallError("timeout"))
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 504
        assert response.json() == {"detail": "telegram timeout"}

    async def test_network_maps_to_502(self) -> None:
        caller = FakeMethodCaller(error=TelegramCallError("network"))
        async with make_client(FakeBotConfig(), caller) as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 502
        assert response.json() == {"detail": "telegram unreachable"}


class TestBodyLimit:
    async def test_oversized_body_413_without_call(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller, max_body_bytes=8) as client:
            response = await client.post(proxy_url(), json={"text": "way too long"})
        assert response.status_code == 413
        assert response.json() == {"detail": "request body too large"}
        assert caller.calls == []

    async def test_chunked_oversized_body_413_after_read(self) -> None:
        # No Content-Length on a streaming request: the limit is enforced
        # on the actual body after reading (research R6).
        caller = FakeMethodCaller()

        async def stream() -> AsyncIterator[bytes]:
            yield b"x" * 16  # > max_body_bytes=8, single chunk

        async with make_client(FakeBotConfig(), caller, max_body_bytes=8) as client:
            response = await client.post(
                proxy_url(), content=stream(), headers={"Content-Type": JSON_CT}
            )
        assert response.status_code == 413
        assert caller.calls == []

    async def test_body_within_limit_passes(self) -> None:
        caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller, max_body_bytes=64) as client:
            response = await client.post(proxy_url(), json={})
        assert response.status_code == 200
        assert len(caller.calls) == 1


class TestParseAllowedMethods:
    def test_default_parses_to_lowercase_set(self) -> None:
        allowed = parse_allowed_methods(DEFAULT_TELEGRAM_PROXY_ALLOWED_METHODS)
        assert "getme" in allowed
        assert "answercallbackquery" in allowed
        assert "sendphoto" not in allowed
        assert len(allowed) == 13

    def test_names_are_trimmed_and_lowercased(self) -> None:
        assert parse_allowed_methods(" GetMe , sendPhoto ") == {"getme", "sendphoto"}

    def test_garbage_element_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid Telegram proxy method"):
            parse_allowed_methods('getMe,send pho"to')

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            parse_allowed_methods("")

    def test_policy_banned_wins_over_allowed(self) -> None:
        allowed = parse_allowed_methods("getUpdates,getMe")
        assert resolve_method_policy("getUpdates", allowed) == "banned"
        assert resolve_method_policy("getMe", allowed) == "ok"
        assert resolve_method_policy("sendPhoto", allowed) == "not_allowed"


class TestNoSecretLeaks:
    """US2: the token, the API key, and bodies never reach responses or
    logs. The refusal details are static literals asserted verbatim in
    the suites above; passthrough Telegram bodies are exempt by design
    (Telegram never echoes tokens; byte identity is FR-002)."""

    async def _run_full_matrix(self, caplog: pytest.LogCaptureFixture) -> None:
        scenarios: list[FakeMethodCaller] = [
            FakeMethodCaller(),  # 200
            FakeMethodCaller(
                result=TelegramMethodResponse(400, JSON_CT, b'{"ok":false}')
            ),
            FakeMethodCaller(error=TelegramCallError("timeout")),
            FakeMethodCaller(error=TelegramCallError("network")),
        ]
        with caplog.at_level(logging.INFO, logger="gram_mq.api"):
            for caller in scenarios:
                async with make_client(FakeBotConfig(), caller) as client:
                    await client.post(
                        proxy_url(method="editMessageText"),
                        json={"text": "cached secret body"},
                    )
            # Policy and token refusals too.
            async with make_client(FakeBotConfig(), FakeMethodCaller()) as client:
                await client.post(proxy_url(method="getUpdates"), json={})
                await client.post(proxy_url(slug="ghost"), json={})

    async def test_token_key_and_body_never_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._run_full_matrix(caplog)
        logged = "\n".join(record.message for record in caplog.records)
        assert TOKEN not in logged
        assert API_KEY not in logged
        assert "cached secret body" not in logged

    async def test_no_secret_in_any_record_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._run_full_matrix(caplog)
        everything = "".join(str(record.getMessage()) for record in caplog.records)
        assert TOKEN not in everything


class TestLogging:
    async def test_success_line_has_bot_method_status(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caller = FakeMethodCaller()
        with caplog.at_level(logging.INFO, logger="gram_mq.api"):
            async with make_client(FakeBotConfig(), caller) as client:
                await client.post(proxy_url(), json={})
        proxy_lines = [r for r in caplog.records if "proxy" in r.message]
        assert len(proxy_lines) == 1
        line = proxy_lines[0].message
        assert f"bot={SLUG}" in line
        assert "method=getMe" in line
        assert "status=200" in line
        assert "dur=" in line
        assert proxy_lines[0].levelname == "INFO"

    async def test_denial_line_warns_with_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caller = FakeMethodCaller()
        with caplog.at_level(logging.INFO, logger="gram_mq.api"):
            async with make_client(FakeBotConfig(), caller) as client:
                await client.post(proxy_url(method="sendPhoto"), json={})
        proxy_lines = [r for r in caplog.records if "proxy" in r.message]
        assert len(proxy_lines) == 1
        assert "reason=method_not_allowed" in proxy_lines[0].message
        assert proxy_lines[0].levelname == "WARNING"

    async def test_not_ready_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        caller = FakeMethodCaller()
        config = FakeBotConfig(ready={}, not_ready=frozenset({SLUG}))
        with caplog.at_level(logging.INFO, logger="gram_mq.api"):
            async with make_client(config, caller) as client:
                await client.post(proxy_url(), json={})
        proxy_lines = [r for r in caplog.records if "proxy" in r.message]
        assert "reason=not_ready" in proxy_lines[0].message

    async def test_transport_failure_line_has_kind(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caller = FakeMethodCaller(error=TelegramCallError("timeout"))
        with caplog.at_level(logging.INFO, logger="gram_mq.api"):
            async with make_client(FakeBotConfig(), caller) as client:
                await client.post(proxy_url(), json={})
        proxy_lines = [r for r in caplog.records if "proxy" in r.message]
        assert "kind=timeout" in proxy_lines[0].message


class TestResponseTimeBudget:
    async def test_every_answer_within_one_second(self) -> None:
        # SC-001: the system's overhead on top of the Telegram call must
        # fit into 1 s. Measures the handler pipeline (policy + token
        # lookup + body checks + mapping + logging) over an instant fake
        # port — the predecessor of the TestResponseTimeBudget suite in
        # test_webhook_endpoint.py; a regression that adds synchronous
        # work to the path fails here before it reaches production.
        caller = FakeMethodCaller()
        denial_caller = FakeMethodCaller()
        async with make_client(FakeBotConfig(), caller) as client:
            for _ in range(50):
                started = time.monotonic()
                response = await client.post(proxy_url(), json={})
                elapsed = time.monotonic() - started
                assert response.status_code == 200
                assert elapsed < 1.0, f"success path took {elapsed:.3f}s"
        async with make_client(FakeBotConfig(), denial_caller) as client:
            for _ in range(50):
                started = time.monotonic()
                response = await client.post(proxy_url(method="sendPhoto"), json={})
                elapsed = time.monotonic() - started
                assert response.status_code == 403
                assert elapsed < 1.0, f"denial path took {elapsed:.3f}s"
