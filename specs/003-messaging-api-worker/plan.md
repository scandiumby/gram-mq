# Implementation Plan: HTTP API приёма сообщений и доставка в Telegram

**Branch**: `003-messaging-api-worker` | **Date**: 2026-10-11 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/003-messaging-api-worker/spec.md`

## Summary

Машинный вход и живая доставка: FastAPI-приложение с POST /api/v1/messages
(авторизация X-API-Key, проверка бота самим контрактом брокера через
UnknownBotError → 404), выдача ключей CLI `gram-mq key add` (открытый ключ
печатается один раз, в БД prefix + Argon2id-хэш), воркер-процесс с циклом
claim → send (порт TelegramSender, адаптер aiogram 3, токен из
`bots/.env-<bot_slug>` через порт BotConfig) → атомарный ack, retry с
backoff для 429/сети, dead_letter для permanентных отказов Telegram.
Подсчёт доставленных — SQL по журналу фичи 001. Всё строится поверх
BrokerPort/PostgresBroker фичи 001; схема расширяется одной таблицей
`gmq_api_keys` (одна Alembic-ревизия).

## Technical Context

**Language/Version**: Python 3.12+ (существующий uv-проект).

**Primary Dependencies (новые)**:
- `fastapi` (MIT) + `uvicorn` (BSD-3) — HTTP API; названы конституцией (Stack).
- `argon2-cffi` (MIT; bundled C-реализация Argon2 — CC0/Apache-2.0) —
  хэширование ключей; назван конституцией (III).
- `aiogram>=3` (MIT; тянет aiohttp Apache-2.0, certifi MPL-2.0) — только
  `Bot`; назван конституцией (Stack).
- dev: `httpx` (BSD-3) — тесты API через ASGITransport.

**Storage**: PostgreSQL — существующий журнал сообщений + новая таблица
`gmq_api_keys` (prefix PK, key_hash, created_at, revoked_at) одной
Alembic-ревизией; файлов конфигурации ботов не касается.

**Testing**: pytest + pytest-asyncio; фейковый TelegramSender и
FileBotConfig-подмена (конституция Testing); httpx ASGITransport; DB-гейты
через GRAMMQ_TEST_DATABASE_URL/TEST_DATABASE_URL, без БД — skip.

**Target Platform**: Linux-сервер, systemd (api и worker — отдельные
юниты, env-файл указателем); разработка на macOS.

**Project Type**: web-service (API) + worker-процесс + дополнение CLI.

**Performance Goals**: постановка сообщения — единственный синхронный
путь API (валидация + INSERT); воркер — один инстанс, poll-interval 1 c;
ключ — Argon2id-верификация (~десятки мс, через to_thread).

**Constraints**: никакой телеграм-сети в обработке запроса API (FR-005);
открытый ключ/токен не попадают в логи, ответы и БД (FR-008/FR-009);
at-least-once, атомарный ack (конституция II).

**Scale/Scope**: один внешний потребитель, десятки сообщений в минуту;
rate limiting, квоты и несколько воркеров — Future.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Принцип/стандарт | Статус | Обоснование |
|---|---|---|
| I. Ports and adapters | ✅ | Новые порты объявлены в спеке через Spec Kit: `TelegramSender`, `BotConfig` (ports/), адаптеры aiogram/file — за ними. Домен (api/worker) зависит только от портов; `Delivery` не инспектируется. |
| II. At-least-once (NON-NEGOTIABLE) | ✅ | Воркер: claim (аренда/attempts из контракта брокера), ack атомарен (`sent`+`telegram_message_id` одним вызовом `BrokerPort.ack` — транзакция внутри адаптера), retry `available_at=now()+backoff`, после max_attempts — `dead_letter` (=`failed`). Разрыв send→ack закрывается lease expiry + reclaim; дубль в чате допустим и документирован. API не ждёт Telegram. |
| III. No plaintext secrets | ✅ | Ключ: Argon2id-хэш + prefix в БД, печать один раз (FR-001/FR-009); токен бота — только чтение из файла воркером (FR-008). `bots/` вне git (сделано в 002). |
| IV. Two realms | ✅ | Только машинный realm: X-API-Key на /api/v1/*, кроме health; человеческих маршрутов не появляется (FR-002/FR-011). |
| Asynchrony | ✅ | uvicorn/FastAPI/aiogram/asyncpg — async; Argon2id-верификация — CPU-bound без async API → `asyncio.to_thread` с комментарием-обоснованием в месте вызова. CLI `key add` — не runtime-код. |
| Type checking | ✅ | mypy --strict на всём новом коде; гейт `uv run check`. |
| Database migrations | ✅ | Таблица api_keys — только Alembic-ревизией; применение схемы — шаг деплоя перед стартом сервисов (quickstart фиксирует; в юнитах ExecStartPre не дублируется). |
| Testing | ✅ | Публичные функции покрыты; фейковый TelegramSender; покрытие ≥80%; секретов и deploy-env в тестах нет; быстрые тесты включают хэширование ключей (конституция Testing). |
| Dependency licenses | ✅ | Новые: fastapi/starlette/pydantic — MIT; uvicorn — BSD-3; argon2-cffi — MIT; aiohttp — Apache-2.0; httpx — BSD-3. Оговаривается: aiogram тянет certifi (MPL-2.0) — file-scoped слабый копилефт, вне запрещённой семьи GPL/AGPL; трактуется как permissive-совместимый, отметка в research.md (R9). |
| Configuration | ✅ | Новые поля в `Settings` (не секреты, с дефолтами): api_host, api_port, worker_poll_interval, worker_backoff_base_seconds. Все entry point'ы читают тот же канал. |
| Stack and deployment | ✅ | FastAPI, aiogram 3 (Bot only), systemd api+worker отдельными юнитами, env-файл — единственная строка-указатель (FR-013). |

Нарушений нет; Complexity Tracking не требуется.

## Project Structure

### Documentation (this feature)

```text
specs/003-messaging-api-worker/
├── plan.md                    # This file ($speckit-plan command output)
├── research.md                # Phase 0 output ($speckit-plan command)
├── data-model.md              # Phase 1 output ($speckit-plan command)
├── quickstart.md              # Phase 1 output ($speckit-plan command)
├── contracts/
│   ├── http-api.md            # Контракт REST API (для внешнего сервиса)
│   ├── telegram-sender.md     # Контракт порта TelegramSender
│   └── bot-config.md          # Контракт порта BotConfig
└── tasks.md                   # Phase 2 output ($speckit-tasks command)
```

### Source Code (repository root)

```text
src/gram_mq/
├── api/
│   ├── __init__.py
│   ├── app.py             # create_app(broker, key_verifier): /api/v1/messages, /health
│   ├── schemas.py         # MessageIn (bot_slug, chat_id, text) — валидация FR-004
│   ├── auth.py            # ApiKeyVerifier: prefix lookup + Argon2id (to_thread)
│   ├── main.py            # gram-mq-api: сборка адаптеров + uvicorn.run(factory)
│   └── __main__.py        # python -m gram_mq.api — тонкая обёртка над main
├── worker/
│   ├── __init__.py
│   ├── runner.py          # run_worker: claim → send → ack/retry/dead_letter
│   ├── bot_config.py      # порт BotConfig + адаптер FileBotConfig (bots/.env-*)
│   ├── main.py            # gram-mq-worker: сборка адаптеров и запуск
│   └── __main__.py        # python -m gram_mq.worker — тонкая обёртка над main
├── ports/telegram.py      # TelegramSender + TelegramSendError (таксономия ошибок)
├── adapters/telegram/aiogram_sender.py  # aiogram 3 Bot; маппинг исключений
├── models/api_keys.py     # ApiKey (prefix PK, key_hash, created_at, revoked_at)
├── cli/keys.py            # генерация ключа + gram-mq key add
└── alembic/versions/      # + одна ревизия: gmq_api_keys

deploy/
├── gram-mq-api.service    # systemd: Environment=GRAMMQ_ENV_FILE=... (указатель)
└── gram-mq-worker.service

tests/
├── unit/                  # api (app/auth/schemas), worker, keys, aiogram-маппинг
└── integration/           # API+реальная БД, worker+реальный брокер (DB-gated)
```

**Structure Decision**: single project (`src/`, `tests/` от корня, фича 001);
новые пакеты `api/` и `worker/`, новые порты в `ports/`, адаптер aiogram в
`adapters/telegram/`, юниты деплоя в `deploy/` (новый каталог — часть
эксплуатационного контракта конституции, не код).

## Complexity Tracking

> Не заполняется: нарушений Constitution Check нет.
