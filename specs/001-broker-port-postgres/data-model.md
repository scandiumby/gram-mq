# Data Model: Очередь исходящих сообщений за портом брокера

Все объекты БД получают префикс из `GRAMMQ_TABLE_PREFIX` (по умолчанию
`gmq`; ниже имена указаны логически). Префикс фиксируется при накате первой
миграции: переопределение на существующей базе не поддерживается (RQ-11).

## Сущности

### bots (→ gmq_bots)

| Поле | Тип | Ограничения | Описание |
| --- | --- | --- | --- |
| bot_slug | string | PK | Единый идентификатор бота (см. фичу bot-management) |
| created_at | timestamptz | NOT NULL, default now() | Метка регистрации |

Токенов и настроек в таблице нет (конституция, принцип III).

### messages (→ gmq_messages)

| Поле | Тип | Ограничения | Описание |
| --- | --- | --- | --- |
| id | UUID | PK | Генерируется приложением (uuid4) при enqueue |
| bot_slug | string | FK → bots.bot_slug, NOT NULL | Чья очередь |
| chat_id | string | NOT NULL | Адресат в Telegram |
| payload | JSONB | NOT NULL | Текст и форматирование (v1: text, parse_mode) |
| status | enum | NOT NULL | queued / leased / sent / failed |
| available_at | timestamptz | NOT NULL | Момент, с которого строку можно захватить |
| locked_at | timestamptz | NULL | Когда взят (последний захват) |
| locked_by | string | NULL | Кто взял (worker_id) |
| lease_expires_at | timestamptz | NULL | Срок владения |
| attempts | int | NOT NULL, default 0 | Счётчик попыток доставки |
| max_attempts | int | NOT NULL, default 5 (RQ-2) | Лимит на строке |
| telegram_message_id | string | NULL | Доказательство доставки |
| error | text | NULL | Причина последнего сбоя / dead_letter |
| created_at | timestamptz | NOT NULL | Постановка |
| sent_at | timestamptz | NULL | Подтверждённая доставка |

### bot_claim_state (справедливость, RQ-4) (→ gmq_bot_claim_state)

| Поле | Тип | Ограничения | Описание |
| --- | --- | --- | --- |
| bot_slug | string | PK, FK → bots | Бот |
| last_claim_at | timestamptz | NULL | Последний захват строки этого бота |

## Индексы (RQ-6)

- `messages(status, available_at) WHERE status='queued'` — выбор кандидата;
- `messages(lease_expires_at) WHERE status='leased'` — реклейм истёкших;
- `messages(bot_slug) WHERE status IN ('queued','leased')` — queue_depth.

## Диаграмма состояний

```text
                 enqueue
                   │
                   ▼
               ┌───────┐   claim (queued, available_at<=now)
               │ queued│◀───────────────────────────┐
               └───┬───┘                             │
                   │ claim                           │ retry (available_at=now+delay,
                   ▼                                 │        attempts+=1, сброс лиза)
               ┌───────┐  истёк lease_expires_at     │
        ┌─────│ leased │─────────────────────────────┤
        │     └───┬───┘   (реклейм тем же claim)    │
        │         │                                 │
        │ ack     │ dead_letter / max_attempts      │
        │ (атомарно: telegram_message_id            │
        │  + sent одной транзакцией)                │
        ▼         ▼                                 │
   ┌──────┐   ┌──────┐                              │
   │ sent │   │failed│                              │
   └──────┘   └──────┘  терминальные: claim не отдаёт, строка остаётся в журнале
```

Примечание: реклейм переводит leased → leased (новый владелец, новый лиз);
на диаграмме показан возврат в работу через то же условие claim (RQ-5).

## Правила переходов и валидации

- enqueue: только в queued, available_at = now(), attempts = 0; bot_slug
  обязан существовать (FK).
- claim: queued с наступившим available_at ИЛИ leased с истёкшим лизом →
  leased (locked_at/locked_by/lease_expires_at = now + лиз, RQ-1);
  справедливость по bot_claim_state (RQ-4).
- ack: leased → sent, атомарно с telegram_message_id и sent_at; чужой или
  завершённой строки — ошибка (RQ-9).
- retry: leased → queued, available_at = now + delay (задача воркера, RQ-3),
  attempts += 1, лиз сбрасывается.
- dead_letter: leased/queued → failed с reason; терминально.
- queue_depth: COUNT(status IN (queued, leased)) по bot_slug (FR-009).
