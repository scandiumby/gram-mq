"""HttpxTelegramMethodCaller unit contract (spec 008: US1/US2, research R3).

httpx MockTransport — the adapter never touches the network here. The
invariants: the URL is built from token+method, the body and Content-Type
travel verbatim (JSON and multipart), answers map as-is, Retry-After
parses, timeouts/network become the two-kind taxonomy, and the token
never survives inside exception texts (Constitution III).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from gram_mq.adapters.telegram.httpx_method_caller import HttpxTelegramMethodCaller
from gram_mq.ports.telegram import TelegramCallError, TelegramMethodResponse

TOKEN = "6789012345:AAE-very-secret-token-value"
METHOD = "getMe"
JSON_CT = "application/json"


def make_caller(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpxTelegramMethodCaller:
    transport = httpx.MockTransport(handler)
    return HttpxTelegramMethodCaller(client=httpx.AsyncClient(transport=transport))


class TestRequestBuilding:
    async def test_url_carries_token_and_method(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["content_type"] = request.headers["Content-Type"]
            return httpx.Response(200, json={"ok": True, "result": {}})

        caller = make_caller(handler)
        await caller.call_method(
            bot_token=TOKEN, method=METHOD, content_type=JSON_CT, body=b"{}"
        )

        assert seen["url"] == f"https://api.telegram.org/bot{TOKEN}/{METHOD}"
        assert seen["content_type"] == JSON_CT

    async def test_body_passes_verbatim(self) -> None:
        seen: dict[bytes, None] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen[request.read()] = None
            return httpx.Response(200, content=b'{"ok":true}')

        caller = make_caller(handler)
        payload = 'текст с unicode "quotes"'.encode()
        await caller.call_method(
            bot_token=TOKEN,
            method="editMessageText",
            content_type=JSON_CT,
            body=payload,
        )

        assert list(seen) == [payload]

    async def test_multipart_content_type_travels(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["content_type"] = request.headers["Content-Type"]
            return httpx.Response(200, content=b"{}")

        caller = make_caller(handler)
        multipart_ct = "multipart/form-data; boundary=abc123"
        await caller.call_method(
            bot_token=TOKEN,
            method="sendPhoto",
            content_type=multipart_ct,
            body=b"--abc123--",
        )

        assert seen["content_type"] == multipart_ct


class TestResponseMapping:
    async def test_answer_maps_as_is(self) -> None:
        body = b'{"ok":true,"result":{"id":42}}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=body, headers={"Content-Type": "application/json"}
            )

        caller = make_caller(handler)
        result = await caller.call_method(
            bot_token=TOKEN, method=METHOD, content_type=JSON_CT, body=b"{}"
        )

        assert result == TelegramMethodResponse(
            status=200, content_type="application/json", body=body
        )

    async def test_error_answer_keeps_status_and_body(self) -> None:
        body = (
            b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, content=body)

        caller = make_caller(handler)
        result = await caller.call_method(
            bot_token=TOKEN, method="sendMessage", content_type=JSON_CT, body=b"{}"
        )

        assert result.status == 400
        assert result.body == body

    async def test_retry_after_header_parses(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, content=b"{}", headers={"Retry-After": "7"})

        caller = make_caller(handler)
        result = await caller.call_method(
            bot_token=TOKEN, method=METHOD, content_type=JSON_CT, body=b"{}"
        )

        assert result.status == 429
        assert result.retry_after == 7.0

    async def test_non_numeric_retry_after_is_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429, content=b"{}", headers={"Retry-After": "next tuesday"}
            )

        caller = make_caller(handler)
        result = await caller.call_method(
            bot_token=TOKEN, method=METHOD, content_type=JSON_CT, body=b"{}"
        )

        assert result.retry_after is None


class _Timeoutish(httpx.TimeoutException):
    pass


class TestTransportFailures:
    async def test_timeout_maps_to_kind(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise _Timeoutish(f"timed out talking to bot{TOKEN}/getMe")

        caller = make_caller(handler)
        with pytest.raises(TelegramCallError) as excinfo:
            await caller.call_method(
                bot_token=TOKEN, method=METHOD, content_type=JSON_CT, body=b"{}"
            )

        assert excinfo.value.kind == "timeout"
        assert TOKEN not in str(excinfo.value.__cause__)

    async def test_network_error_maps_to_kind_and_redacts(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach api.telegram.org/bot{TOKEN}")

        caller = make_caller(handler)
        with pytest.raises(TelegramCallError) as excinfo:
            await caller.call_method(
                bot_token=TOKEN, method=METHOD, content_type=JSON_CT, body=b"{}"
            )

        assert excinfo.value.kind == "network"
        assert TOKEN not in str(excinfo.value.__cause__)
        assert "<redacted>" in str(excinfo.value.__cause__)


class TestLifecycle:
    async def test_owned_client_closes(self) -> None:
        caller = HttpxTelegramMethodCaller()
        await caller.aclose()
        assert caller._client.is_closed

    async def test_injected_client_survives_aclose(self) -> None:
        client = httpx.AsyncClient()
        caller = HttpxTelegramMethodCaller(client=client)
        await caller.aclose()
        assert not client.is_closed
        await client.aclose()
