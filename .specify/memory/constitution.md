# Gram-MQ Constitution

<!--
Sync Impact Report (временная заметка для ревью — удалить перед коммитом)
- Version change: 2.0.2 → 2.1.0 (MINOR: норма ужесточена — с «один раз
  в ответе POST» до «никогда через HTTP»; принцип не переопределён,
  поэтому не MAJOR)
- Modified principles:
  - III — plaintext API-ключа больше не выдаётся в HTTP-ответе на создание:
    выпуск только CLI на сервере с печатью один раз в терминале;
    эндпоинты возвращают лишь prefix + key_hash
- Unchanged by design: принцип II (at-least-once) — постфактум-проверка
  доставки через Bot API невозможна; сужение неопределённости (статус
  sending, лог tg_accepted, реконсилятор) — материал spec/plan воркера
- Added sections: нет
- Removed sections: нет
- Deferred TODOs: выпуск ключа CLI, страница ключей без POST-выдачи и
  механизмы реконсиляции — в будущие spec фич ($speckit-specify)
-->

## Core Principles

### I. Порты и адаптеры, а не конкретные технологии

Домен (API, worker, дашборд) зависит только от портов: `BrokerPort`,
`TelegramSender`, `AuthProvider`, `BotConfig`. Какая реализация под ними —
`PostgresBroker`, `RabbitBroker`, in-memory, aiogram, fake — домену неизвестно.

- `BrokerPort` — доменный контракт (`enqueue` / `claim` / `ack` / `retry` /
  `dead_letter` / `queue_depth`), не AMQP. Новые методы порта появляются
  только когда нужны конкретному адаптеру (пример: `ensure_bot_queue`
  для RabbitMQ).
- `Delivery` — непрозрачный handle адаптера; домен его не разбирает.
- Новый адаптер добавляется только через спецификацию Spec Kit
  (spec → plan → tasks), а не «по ходу задач».

Обоснование: смена брокера, провайдера входа или Telegram-клиента — это новый
адаптер, а не переписывание FastAPI, worker и дашборда.

### II. Сообщение не теряется — доставка at-least-once (NON-NEGOTIABLE)

Каждое принятое сообщение обязано пережить падение любого процесса.

- Лиз (`lease_expires_at`) и счётчик `attempts` обязательны; «надеемся на
  systemd» запрещено. Падение worker не теряет строки: они остаются `queued`
  или снова доступны после истечения лиза.
- Ack атомарен: `telegram_message_id` и `status=sent` пишутся в одной
  транзакции сразу после успешного ответа Telegram.
- Доставка at-least-once, не exactly-once: дубль в чат при разрыве между send
  и commit допустим и документирован. Это свойство контракта, не дефект схемы.
- Retry (429, сеть) — `available_at = now() + backoff`, `attempts += 1`;
  после `max_attempts` — `failed` (аналог DLQ).
- API и worker — разные процессы: HTTP-запрос никогда не ждёт Telegram.
  Лимиты Telegram живут в worker, не в брокере.

### III. Plaintext-секрета в системе нет

- В БД секретов нет — только хэши: API-ключи и пароли — Argon2id, OTP и
  refresh — HMAC-SHA256.
- Токен бота хранится только в `bots/.env-<bot_slug>` на диске; никогда —
  в SQL, в ответах API или дашборда. Нет файла — сообщения этим ботом
  не отправляются.
- Plaintext API-ключа никогда не пересекает HTTP: ключ выпускается только
  CLI на сервере и печатается один раз в терминале. Эндпоинты API и
  дашборда возвращают лишь `prefix` + `key_hash`. Отзыв — `revoked_at`;
  ротация — новый ключ плюс revoke старого.
- Access-токен — в httpOnly cookie, не в localStorage. OTP не логируется.
- Каталог `bots/` не коммитится (`.gitignore`).

### IV. Два контура авторизации, отдельные пространства URL

- Люди: `AuthProvider` (v1 — `TelegramOtpProvider`) → JWT в httpOnly cookie →
  `/api/dashboard/*`. Машины: заголовок `X-API-Key` на весь `/api/v1/*`
  (кроме health). Машины не используют dashboard-URL, люди — не используют
  `/api/v1/*` по ключу.
- Смена способа входа (пароль, телефон, SMS) = новый адаптер `AuthProvider`
  плюс колонка/форма, а не новый «логин-сервер». Выдача JWT, cookie, refresh
  и API-ключи от провайдера не зависят.
- Первого пользователя создаёт администратор (CLI/bootstrap-механизм),
  саморегистрации по HTTP нет. Регистрация ботов по HTTP запрещена —
  только CLI на сервере.

## Технические ограничения (v1)

- Стек: Python 3.12+, uv, FastAPI + Pydantic v2 + pydantic-settings,
  SQLAlchemy 2 (async) + asyncpg + Alembic, PostgreSQL 16 на хосте,
  aiogram 3 (только `Bot`), React + TypeScript + Vite + MUI (MIT).
- Инструменты разработки: ruff, pytest + pytest-asyncio.
- Деплой: systemd (api + worker отдельными юнитами), uv venv на хосте,
  без Docker и Compose. Миграции (`alembic upgrade head`) выполняются до
  старта сервисов.
- Очередь = журнал: `PostgresBroker` работает по таблице `messages`;
  отдельного брокера в v1 нет.
- `bot_slug` — человекочитаемый (slug-safe ASCII) единый идентификатор бота
  во входящем API, `messages` и имени файла конфигурации.

## Требования к тестам

- Юнит-тесты используют in-memory брокер и fake Telegram sender; реальные
  секреты и `.env` в тестах не применяются.
- Тесты с БД запускаются только при заданном `TEST_DATABASE_URL`, иначе
  пропускаются.
- Быстрые тесты обязаны покрывать поведение порта, хэши ключей/OTP и
  генерацию slug.

## Governance

- Конституция главнее материала отдельных фич и локальных практик: при
  конфликте побеждает она.
- Генерирующие команды Spec Kit (specify, clarify, plan, checklist, tasks,
  implement) обязаны читать принципы конституции как входные ограничения
  своих артефактов.
- Гейты соответствия: **plan** заполняет Constitution Check и завершается
  ERROR при неоправданном нарушении; **analyze** помечает конфликт
  требования с MUST-принципом как CRITICAL; **converge** порождает task на
  исправление нарушенного принципа. Ревью-гейты флоу после spec и plan —
  точки человеческой проверки.
- Поправки вносятся только отдельным явным обновлением конституции
  ($speckit-constitution): версия по semver — MAJOR при удалении или
  переопределении принципа, MINOR при добавлении принципа или существенном
  расширении, PATCH при уточнении формулировок; каждая правка обновляет
  дату `Last Amended`.
- Команды analyze и converge конституцию только читают и не изменяют.

**Version**: 2.1.0 | **Ratified**: 2026-09-23 | **Last Amended**: 2026-09-26
