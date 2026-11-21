# Contract: InboundBrokerPort — очередь входящих апдейтов

**Spec**: FR-003, FR-005; конституция II | **Research**: R3, R4

`src/gram_mq/ports/inbound.py`. Сиблинг `BrokerPort` (фича 001) с
входящими типами; адаптеры — `PostgresInboundBroker` (журнал =
очередь, таблица `gmq_inbound_updates`) и `InMemoryInboundBroker`
(тестовый дубль). Семантика идентична исходящему порту, различия —
только в данных.

## Типы

```python
class InboundUpdate:      # постановка
    id: UUID              # генерирует продюсер (эндпоинт)
    bot_slug: str
    update_id: int        # Telegram update_id
    payload: dict[str, Any]   # апдейт как есть, без интерпретации

class InboundDelivery:    # результат claim, opaque-хендл воркера
    id: UUID
    bot_slug: str
    update_id: int
    payload: dict[str, Any]
    context: dict[str, Any]   # подсказки (attempts); воркер не инспектирует
```

`UnknownBotError`, `IllegalDeliveryStateError` — те же классы, что у
`BrokerPort`.

## Методы

- `enqueue_inbound(update) -> bool` — записать строку
  `status=queued, available_at=now(), attempts=0`; возвращает True,
  когда создана новая запись, и False, когда идентичный ретрай поглощён
  (по этому флагу эндпоинт логирует поглощение, FR-010). Поднимает
  `UnknownBotError`, когда bot_slug не в реестре (эндпоинт отображает в
  404; проверка — явный SELECT, FK нет — журнал переживает ревок).
  Идемпотентна по (bot_slug, update_id, payload): точное совпадение с
  существующей записью поглощается без артефакта (ретраи Telegram не
  множат очередь), то же update_id с иным payload принимается новой
  записью (research R13).
- `claim(*, worker_id) -> InboundDelivery | None` — эксклюзивное
  владение: кандидаты — `queued` с наступившим `available_at` плюс
  `delivering` с истёкшим lease (реклейм краша); справедливость —
  round-robin по `gmq_bot_inbound_claim_state` (бот с огромной очередью
  не морит остальных). None — работы нет.
- `ack(delivery) -> None` — атомарная фиксация доставки:
  `status=delivered, delivered_at=now()` одним UPDATE;
  `IllegalDeliveryStateError`, если строка не в lease воркера.
- `retry(delivery, delay, reason, *, count_attempt=True) -> None` —
  вернуть в `queued`: `available_at=now()+delay`, `error=reason`,
  `attempts += 1`; `count_attempt=False` — 429-пауза (ожидание
  retry_after не считается неудачной попыткой).
- `dead_letter(delivery, reason) -> None` — `status=failed, error=reason`,
  терминально; строка остаётся в журнале.
- `queue_depth(bot_slug) -> int` — число недоставленных
  (`queued` + `delivering`) апдейтов бота.

## Инварианты

- Право менять строку — только у владельца lease (все переходы через
  статусную проверку в одном UPDATE, как у `PostgresBroker._transition`).
- Краш между POST наружу и ack закрывается истечением lease + реклеймом —
  повторная доставка возможна: at-least-once, дубль документирован.
- `max_attempts` фиксируется в строке при постановке (снапшот настройки).
