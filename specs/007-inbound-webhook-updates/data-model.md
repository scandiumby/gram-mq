# Data Model: Приём вебхуков Telegram и доставка апдейтов внешнему сервису

**Date**: 2026-11-21 | **Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)

Миграция `alembic/versions/20261121_0004_inbound_updates.py` — единственная
правка схемы; имена — через `table_name()` (префикс `GRAMMQ_TABLE_PREFIX`,
по умолчанию `gmq`). Образец механики — `gmq_messages`/`gmq_bot_claim_state`
из 0001 с изменениями под входящий поток (research R4).

## Сущности

### `gmq_inbound_updates` — NEW: журнал входящих = очередь (Constitution II)

| Поле | Тип | NULL | Назначение |
|---|---|---|---|
| id | UUID PK | нет | генерирует продюсер (эндпоинт) |
| bot_slug | String(64) | нет | без FK на `gmq_bots` — журнал переживает ревок бота (решение 0003, распространено) |
| update_id | BigInteger | нет | Telegram update_id; дедуп — на стороне потребителя, индекс НЕуникальный |
| payload | JSONB | нет | апдейт как есть, без интерпретации (FR-008) |
| status | enum `gmq_inbound_status` | нет | queued / delivering / delivered / failed |
| available_at | timestamptz | нет | now() при постановке; now()+delay при retry |
| locked_at, locked_by | timestamptz, String | да | владелец lease (воркер) |
| lease_expires_at | timestamptz | да | реклейм крашнувшегося воркера |
| attempts | Integer | нет | 0 при постановке; +1 на retry (кроме 429-паузы) |
| max_attempts | Integer | нет | снапшот `GRAMMQ_BROKER_MAX_ATTEMPTS` на момент постановки |
| error | Text | да | последняя причина (последний retry / терминальный failed) |
| created_at | timestamptz | нет | server_default now() — время приёма |
| delivered_at | timestamptz | да | время успешной доставки (в ack) |

Индексы (партициальные, по горячим запросам — зеркало 0001):

- `ix_gmq_inbound_updates_claim (status, available_at) WHERE status='queued'`
- `ix_gmq_inbound_updates_reclaim (lease_expires_at) WHERE status='delivering'`
- `ix_gmq_inbound_updates_depth (bot_slug) WHERE status IN ('queued','delivering')`
- `ix_gmq_inbound_updates_dupe (bot_slug, update_id)` — **без UNIQUE**:
  дубль законен (at-least-once), индекс для эксплуатации/подсчёта
  повторов и опора мягкого дедупа (research R13): при совпадении ключа
  сверяется payload — точное совпадение записи не создаётся

### `gmq_bot_inbound_claim_state` — NEW: ledger справедливости входящих

| Поле | Тип | Назначение |
|---|---|---|
| bot_slug | String(64) PK | |
| last_claim_at | timestamptz NULL | round-robin: первым берётся бот, ждавший дольше всех |

Отдельно от `gmq_bot_claim_state`: один ledger на два потока ломал бы
справедливость (очереди одного бота конкурировали бы за его строку).

### `gmq_bots` — расширяется

| Новое поле | Тип | NULL | Назначение |
|---|---|---|---|
| webhook_secret_hmac | String(64) | **да** | hex HMAC-SHA256 секрета вебхука под `GRAMMQ_WEBHOOK_MASTER_KEY`; NULL = приём выключен (`webhook delete` или бот без вебхука); умирает вместе со строкой при ревоке — приём гасится сам |

## Статусы и переходы (`gmq_inbound_status`)

```text
                    enqueue (COMMIT = момент успеха 200 Telegram'у)
                          │
                          ▼
  ┌──────────┐   claim (lease)   ┌─────────────┐
  │  queued  │◄──────────────────│ delivering  │
  └──────────┘   retry:          └─────────────┘
        ▲        available_at=    │        │
        │        now()+backoff    │        │
        └─────────────────────────┘        │ ack (атомарно:
              429: то же, attempts не растут │  status + delivered_at)
                                           ▼
                                    ┌───────────┐
                     dead_letter ──►│ delivered │
                     (4xx сразу или  └───────────┘
                      max_attempts)
                                           ▲ терминальные, строка
                                    ┌───────────┐     остаётся в журнале
                                    │  failed   │
                                    └───────────┘
```

Инварианты:

- `queued` c невступившим `available_at` и `delivering` с живым lease
  невидимы для claim; `delivering` с истёкшим lease реклеймится любым
  воркером (at-least-once: повторная доставка возможна, SC-005);
- терминальные статусы (`delivered`, `failed`) никогда не покидают журнал;
- `attempts < max_attempts` — условие retry; исчерпание → `failed` с
  причиной `max attempts exceeded: …`.

## Доменные объекты порта (`ports/inbound.py`)

- `InboundUpdate(id: UUID, bot_slug: str, update_id: int, payload: dict)`
  — вход в очередь; продюсер (эндпоинт) генерирует `id`;
- `InboundDelivery(id, bot_slug, update_id, payload, context: dict)` —
  результат claim; `context["attempts"]` — подсказка для backoff, перед
  ack воркер не пишет в него ничего (у исходящих — telegram_message_id,
  здесь терминальная фиксация — `delivered_at` на стороне адаптера);
- `enqueue_inbound` поднимает `UnknownBotError` (тот же класс, что у
  исходящих) — источник 404 эндпоинта;
- `enqueue_inbound` идемпотентен по (bot_slug, update_id, payload):
  точное совпадение поглощается без записи (эндпоинт всё равно отвечает
  200, а по возвращённому False логирует поглощение — FR-010), иное
  содержимое при том же update_id — новая запись (R13). Метод
  возвращает bool: True — новая строка, False — поглощение.

## Настройки (канал `GRAMMQ_`, класс `Settings`)

| Переменная | Тип/дефолт | Секрет | Потребитель |
|---|---|---|---|
| GRAMMQ_WEBHOOK_PUBLIC_URL | str \| None = None | нет | CLI `webhook set` (база адреса) |
| GRAMMQ_WEBHOOK_MASTER_KEY | SecretStr \| None = None | да | CLI (HMAC при записи), API (HMAC при проверке) |
| GRAMMQ_INBOUND_DELIVERY_URL | str \| None = None | нет | inbound-worker (fail-fast старта), run_all (условие третьего сервиса) |
| GRAMMQ_INBOUND_DELIVERY_AUTH_HEADER_NAME | str = "Authorization" | нет | inbound-worker |
| GRAMMQ_INBOUND_DELIVERY_AUTH_HEADER_VALUE | SecretStr \| None = None | да | inbound-worker |
| GRAMMQ_INBOUND_DELIVERY_TIMEOUT_SECONDS | float = 10.0 | нет | inbound-worker |

`worker_poll_interval`, `worker_backoff_base_seconds`,
`broker_lease_seconds`, `broker_max_attempts` — общие с исходящими
(research R10).
