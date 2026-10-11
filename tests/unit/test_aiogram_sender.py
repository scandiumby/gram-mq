"""Unit contract of the aiogram exception taxonomy (TelegramSender port).

Only the pure `classify` mapping is tested — no network, no Bot instance.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

from gram_mq.adapters.telegram.aiogram_sender import classify


def test_retry_after_is_mapped_with_delay() -> None:
    exc = TelegramRetryAfter(
        method=cast(Any, "sendMessage"),
        message="Too Many Requests: retry after 7",
        retry_after=7,
    )
    error = classify(exc)
    assert error.retry_after == 7.0
    assert error.permanent is False


def test_network_error_is_retryable() -> None:
    exc = TelegramNetworkError(method=cast(Any, "sendMessage"), message="conn reset")
    error = classify(exc)
    assert error.retry_after is None
    assert error.permanent is False


def test_timeout_is_retryable() -> None:
    error = classify(TimeoutError())
    assert error.permanent is False


def test_bad_request_is_permanent() -> None:
    exc = TelegramBadRequest(method=cast(Any, "sendMessage"), message="chat not found")
    error = classify(exc)
    assert error.permanent is True
    assert error.retry_after is None


def test_forbidden_is_permanent() -> None:
    exc = TelegramForbiddenError(method=cast(Any, "sendMessage"), message="bot blocked")
    error = classify(exc)
    assert error.permanent is True


def test_unauthorized_token_is_permanent() -> None:
    exc = TelegramUnauthorizedError(method=cast(Any, "sendMessage"), message="401")
    error = classify(exc)
    assert error.permanent is True


def test_unexpected_error_is_permanent_and_named() -> None:
    error = classify(ValueError("boom"))
    assert error.permanent is True
    assert "ValueError" in error.reason


def test_reason_never_contains_a_token() -> None:
    token = "123456789:AAFleak-check"
    exc = TelegramBadRequest(method=cast(Any, "sendMessage"), message=f"bad {token}")
    error = classify(exc)
    assert token not in error.reason


class _StubSession:
    async def close(self) -> None:
        return None


class _StubMessage:
    message_id = 5


class _OkBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.chat_id = ""
        self.text = ""
        self.session = _StubSession()

    async def send_message(self, *, chat_id: str, text: str) -> _StubMessage:
        self.chat_id = chat_id
        self.text = text
        return _StubMessage()


class _FailingBot(_OkBot):
    async def send_message(self, *, chat_id: str, text: str) -> _StubMessage:
        raise TelegramBadRequest(
            method=cast(Any, "sendMessage"), message="chat not found"
        )


async def test_send_text_returns_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gram_mq.adapters.telegram.aiogram_sender as sender_module

    monkeypatch.setattr(sender_module, "Bot", _OkBot)
    sender_module._bot_cache.clear()
    sender = sender_module.AiogramTelegramSender()

    result = await sender.send_text(bot_token="10:test-secret", chat_id="42", text="hi")

    assert result == "5"
    await sender.aclose()
    assert sender_module._bot_cache == {}


async def test_send_text_maps_exceptions_to_port_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gram_mq.adapters.telegram.aiogram_sender as sender_module
    from gram_mq.ports.telegram import TelegramSendError

    monkeypatch.setattr(sender_module, "Bot", _FailingBot)
    sender_module._bot_cache.clear()
    sender = sender_module.AiogramTelegramSender()

    with pytest.raises(TelegramSendError) as excinfo:
        await sender.send_text(bot_token="10:test-secret", chat_id="42", text="hi")
    assert excinfo.value.permanent is True
    await sender.aclose()
