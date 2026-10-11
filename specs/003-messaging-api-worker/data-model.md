# Data Model: HTTP API приёма сообщений и доставка в Telegram

**Date**: 2026-10-11 | **Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)

## Сущности

### ApiKey — новая таблица `gmq_api_keys` (одна Alembic-ревизия)

| Поле | Тип | Ограничения |
|---|---|---|
| prefix | String(12), PK | первые 12 символов ключа, вкл. `gmq_` |
| key_hash | String(128), NOT NULL | Argon2id-encoded hash (argon2-cffi, ~97 симв.) |
| created_at | timestamptz NOT NULL | server_default now() |
| revoked_at | timestamptz NULL | NULL = действителен; отзыв — эксплуатационный UPDATE |

Открытого ключа в схеме нет (конституция III). Индексов кроме PK не нужно
(lookup только по prefix).

### Message — существует (фича 001), не меняется

Заполняемые этой фичей поля: `status` (queued → leased → sent / failed),
`attempts`, `available_at` (retry-задержки), `telegram_message_id`,
`sent_at`. Схема и миграции — без изменений.

### Диалог с внешним сервисом (не хранится отдельно)

`MessageIn`: bot_slug (1..64), chat_id (`^-?\d{1,18}$` или
`^@[A-Za-z0-9_]{4,64}$`), text (1..4096). Отображается в
`OutboundMessage(id=uuid4(), bot_slug, chat_id, payload={"text": text})`.

## Валидации

- Ключ запроса: заголовок X-API-Key; формат `gmq_[A-Za-z0-9_-]{43}`;
  неверный формат/длина/unknown prefix/не сошёлся хэш/revoked → 401 (без
  различения причины в ответе).
- bot_slug: непустая строка ≤64; существование в реестре проверяет
  `enqueue` (UnknownBotError → 404).
- chat_id: числовой идентификатор чата/канала (возможен минус и @username);
  реальное существование проверяет только Telegram при отправке.
- text: 1..4096 символов (лимит Telegram; спека FR-004).

## Переходы состояний сообщения (жизненный цикл доставки)

```
            enqueue (API, 202)
   ────► queued ──claim──► leased ──send ok──► sent (ack: telegram_message_id,
   ▲         ▲    (аренда)    │                 sent_at — одной транзакцией)
   │         │                ├─ сеть/429/нет файла ──► retry: queued(attempts+1,
   │         │                │                          available_at=now()+delay)
   │         └─ lease expiry ─┤
   │             (reclaim)    └─ permanent Telegram (400/403) ──► failed
   │                                                            (dead_letter, с причиной)
   └── attempts = max_attempts ──► failed (dead_letter, с причиной)
```

Инварианты (конституция II): ack атомарен; дубль после разрыва
send→ack допустим (lease expiry → повторная доставка); потеря строки
исключена (queued/leased живут в журнале; leased недоступен другим до
истечения аренды).

## Идентичность воркера

worker_id = `"{hostname}:{pid}"` — значение владения арендой
(locked_by); между рестартами меняется, reclaim по lease_expires_at не
зависит от него.
