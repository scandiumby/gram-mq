# Contract: TelegramSender (порт домена)

**Date**: 2026-10-11 | **Spec**: [spec.md](spec.md) | Конституция I: домен
зависит только от порта; aiogram — один из адаптеров, фейк — другой.

## Интерфейс

```python
class TelegramSender(Protocol):
    async def send_text(
        self, *, bot_token: str, chat_id: str, text: str
    ) -> str:
        """Send plain text; return the Telegram message id.

        Raises TelegramSendError on any failure. Never returns a partial
        result; no parse_mode in v1 (plain text only).
        """
```

## TelegramSendError

```python
@dataclass(frozen=True)  # семантика полей
class TelegramSendError(Exception):
    reason: str            # человекочитаемая причина БЕЗ токена и текста сообщения
    retry_after: float | None  # сек; только для 429 (Telegram указал retry_after)
    permanent: bool        # True: повтор бессмысленен (400/403 Telegram)
```

Классификация (адаптер обязан её соблюдать; воркер на неё опирается):

| Ситуация | retry_after | permanent | Политика воркера |
|---|---|---|---|
| 429 Too Many Requests | сек из ответа | False | retry строго по retry_after, попытка НЕ расходуется (count_attempt=False; дополнение 2026-10-11) |
| сеть/таймаут/5xx Telegram | None | False | retry с backoff, попытка расходуется |
| 400 BadRequest (chat not found, кривой chat_id…) | None | True | dead_letter немедленно |
| 403 Forbidden (бот заблокирован, кикнут) | None | True | dead_letter немедленно |

## Инварианты

- Токен бота передаётся только в адаптер и не покидает его кроме как в
  Telegram API; в reason ошибок не входит.
- `send_text` не ведёт собственного журнала с секретами; логирует
  (адаптер) только bot_slug/chat_id/исход.
- Успех ⇔ Telegram подтвердил сообщение — возвращённый message_id
  записывается воркером в `telegram_message_id` при атомарном ack.
- Адаптер aiogram: один `aiogram.Bot` на токен (кэш), `aclose()` закрывает
  все сессии; конституция Stack — aiogram 3, `Bot` only.
- Фейк (тесты): детерминированный message_id-счётчик и скриптуемые
  отказы по (bot_slug, chat_id) — см. tests/unit.
