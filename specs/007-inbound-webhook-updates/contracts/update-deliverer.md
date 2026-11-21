# Contract: UpdateDeliverer — доставка апдейта внешнему серверу

**Spec**: FR-004, FR-006; US2 | **Research**: R5, R6

`src/gram_mq/ports/deliverer.py` — сиблинг `TelegramSender` (фича 003):
домен inbound-воркера зависит только от порта; адаптер —
`HttpxUpdateDeliverer` (`src/gram_mq/adapters/http/deliverer.py`),
тестовый fake — второй адаптер. Таксономия ошибок — часть контракта:
политика повторов воркера построена на `retry_after` и `permanent`.

## Порт

```python
class DeliveryError(Exception):
    reason: str                # без секретов (auth-значение редактируется)
    retry_after: float | None  # сервер попросил подождать (429, сек)
    permanent: bool            # повтор бессмыслен

class UpdateDeliverer(Protocol):
    async def deliver_update(self, *, update: dict[str, Any]) -> None:
        """POST payload как есть на внешний эндпоинт.
        Успех — 2xx. Любая неудача — DeliveryError."""
```

## Адаптер httpx: отображение исходов

| Исход запроса | Классификация |
|---|---|
| 2xx | успех |
| 429 (+ Retry-After, если разобрался) | `retry_after=N`, attempts не тратятся |
| таймаут, обрыв соединения, 5xx | повторимо, `permanent=False` |
| прочие 4xx (400/401/403/404/410/…) | `permanent=True` — сразу failed (FR-006) |

Классификация — чистой функцией `classify()` (тестируется без сети, по
образцу aiogram-адаптера). Заголовок авторизации — только когда
`GRAMMQ_INBOUND_DELIVERY_AUTH_HEADER_VALUE` задан
(`…_HEADER_NAME`, дефолт Authorization); значение — SecretStr, в
`reason`/логи не попадает.

## Контракт приёмной стороны (нестабильного сервера, FR-013)

Обязательства потребителя, документируемые этой фичей (реализация — на
его стороне):

- успех (2xx) — только после успешной обработки апдейта: ранний 200 при
  упавшей обработке означал бы потерю (стабильная сторона зафиксирует
  delivered);
- дедуп — скользящим окном по (bot_slug, update_id): дубль доставки
  возможен (крах воркера между POST и ack — неустранимое окно
  at-least-once); вечное множество update_id использовать нельзя —
  Telegram переиспользует id после ≥ недели простоя бота;
- время обработки должно укладываться в таймаут доставки
  (`GRAMMQ_INBOUND_DELIVERY_TIMEOUT_SECONDS`) — иначе медленная, но
  успешная обработка будет принята за таймаут и повторена.

## Политика воркера (inbound_worker/runner.py)

- успех → `broker.ack` (атомарно `delivered` + `delivered_at`);
- `retry_after` → `broker.retry(delay=max(retry_after,1s),
  count_attempt=False)`;
- повторимая ошибка → `delay = min(base * 2**attempts, 300s)` (кап как у
  исходящих), `count_attempt=True`; `attempts+1 >= max_attempts` →
  `dead_letter("max attempts exceeded: …")`;
- `permanent` → `dead_letter(reason)` немедленно.

Воркер не знает адреса, заголовков и клиента — только порт и брокер.
Payload передаётся без модификации (SC-003): ни полей, ни структуры,
ни типов.
