# Implementation Plan: Приём вебхуков Telegram и доставка апдейтов внешнему сервису

**Branch**: `007-inbound-webhook-updates` | **Date**: 2026-11-21 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/007-inbound-webhook-updates/spec.md`

## Summary

Система получает второе направление потока — входящее. Telegram шлёт
апдейты ботов на пер-ботовый эндпоинт `POST /webhooks/{bot_slug}`
(третье URL-пространство рядом с `/api/v1` и `/api/dashboard`),
аутентифицированный секретом вебхука: система сама генерирует секрет при
CLI-регистрации (`gram-mq webhook set`), передаёт его Telegram'у в
`set_webhook(secret_token=…)` и хранит в таблице ботов только
HMAC-SHA256-проверочное значение под выделенным мастер-ключом
`GRAMMQ_WEBHOOK_MASTER_KEY` — plaintext секрета нигде не хранится.
Хендлер делает ровно три вещи — проверка секрета (constant-time),
мягкий дедуп ретрая (совпадение bot_slug + update_id + payload
поглощается без артефакта, успех Telegram'у всё равно возвращается) и
добавление в журнал входящих, после чего Telegram'у уходит немедленный
200 (SC-002 ≤ 1 c); ничего больше в запросе не выполняется, пересылка —
только в фоне. Входящие апдейты живут в собственной таблице-журнале
`gmq_inbound_updates` за новым доменным портом `InboundBrokerPort`
(сиблинг `BrokerPort` с теми же at-least-once гарантиями: lease, attempts,
повторы, failed), которую переживают перезапуски и недоступность внешнего
сервера. Отдельный процесс `gram-mq-inbound-worker` (третий сервис в
`run_all.py`, условный — стартует только при заданном
`GRAMMQ_INBOUND_DELIVERY_URL`) забирает апдейты round-robin по ботам и
доставляет payload без изменений на внешний эндпоинт через новый порт
`UpdateDeliverer` (адаптер httpx): таймаут/429/5xx — повтор с
экспоненциально нарастающей задержкой (cap 300 c, как у исходящих),
остальные 4xx — сразу failed; успешная доставка фиксируется атомарно
(`delivered` + `delivered_at`). Снятие приёма — `gram-mq webhook delete`
(deleteWebhook + очистка проверочного значения); ревок бота убивает приём
вместе со строкой реестра, а журналу и доставке не мешает.

## Technical Context

**Language/Version**: Python 3.12+ (существующий uv-проект).

**Primary Dependencies**: +`httpx` (runtime; BSD-3-Clause — в разрешённом
списке лицензий) для POST-доставки апдейтов; aiogram 3 — уже есть,
`Bot.set_webhook`/`delete_webhook` используются CLI; остальное
существующее: FastAPI, SQLAlchemy 2 (async) + asyncpg + Alembic,
pydantic-settings, cryptography (HMAC идёт через `hashlib`/`hmac` из
stdlib — новых крипто-зависимостей нет), argon2-cffi (не на пути вебхука).

**Storage**: PostgreSQL — миграция `0004`: новая таблица-журнал
`gmq_inbound_updates` (payload JSONB, update_id, статусы
queued/delivering/delivered/failed, lease/attempts, партициальные
индексы claim/reclaim/depth), таблица справедливости
`gmq_inbound_claim_state`, колонка `gmq_bots.webhook_secret_hmac`
String(64) NULL (NULL = приём выключен).

**Testing**: pytest + pytest-asyncio; контрактный прогон нового порта —
`InMemoryInboundBroker` (новый in-memory дубль) и `PostgresInboundBroker`
по `GRAMMQ_TEST_DATABASE_URL`; эндпоинт — httpx ASGITransport + fakes;
доставка — fake `UpdateDeliverer`; CLI — fake Telegram-клиент;
интеграционные против тестовой БД (миграция, жизненный цикл); гейт
`uv run check` (ruff + mypy --strict), покрытие ≥ 80%.

**Target Platform**: Linux-сервер/PaaS-контейнер (Amvera: один контейнер
api + worker + inbound-worker — третий процесс условный, по наличию
`GRAMMQ_INBOUND_DELIVERY_URL`); systemd-деплой — новый юнит
`gram-mq-inbound-worker.service` рядом с существующими.

**Project Type**: web-service (API + два воркера) с CLI-администрированием.

**Performance Goals**: путь вебхука — 1 SELECT (бот + проверочное
значение) + 1 INSERT + COMMIT: единицы миллисекунд, укладывается в
SC-002 (≤ 1 c); Argon2 на пути вебхука запрещён проектным решением R2
(десятки мс на хит) — только HMAC-SHA256 (микросекунды); доставляющий
воркер — десятки апдейтов/с при доступном внешнем сервере.

**Constraints**: немедленный 200 Telegram'у — только после долговременной
фиксации, и ничего синхронного кроме SELECT+INSERT (FR-002); payload
пересылается побитово без модификации (SC-003); экспоненциальный backoff
без лишней нагрузки на лежащий сервер (R5); секрет вебхука — открытый
текст только кратко в памяти CLI в момент `set_webhook` (конституция III);
все новые настройки — через `GRAMMQ_`-канал, секреты — `SecretStr`.

**Scale/Scope**: десятки ботов; сотни апдейтов/мин; одна целевая точка
доставки на инстанс (per-бот маршрутизация — Future Enhancements).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Конституция v3.0.0. Спецификация и операторские ограничения (сессия
2026-11-21 (2)) уже согласованы с ней; единственное расширение —
постоянное третье URL-пространство `/webhooks/*` — требует MINOR-правки
Principle IV (задача на фазу implement, правка только через
`$speckit-constitution`, как это сделала фича 006 с v3.0.0).

| Принцип/стандарт | Статус | Обоснование |
|---|---|---|
| I. Ports and adapters | ✅ | Домен получает два новых порта: `InboundBrokerPort` (`ports/inbound.py` — enqueue_inbound/claim/ack/retry/dead_letter/queue_depth, сиблинг `BrokerPort` с inbound-типами данных; переиспользовать сам `BrokerPort` нельзя — его контракт исходящий: `chat_id`, `telegram_message_id` в ack) и `UpdateDeliverer` (`ports/deliverer.py` — сиблинг `TelegramSender` с таксономией DeliveryError). Адаптеры: `PostgresInboundBroker`, `InMemoryInboundBroker`, `HttpxUpdateDeliverer`, `SqlAlchemyWebhookAuth`; чистый модуль `webhook_store.py` (генерация секрета, HMAC, constant-time сверка) — без технологий, по образцу `token_store.py`. API-домен получает инъектируемые порты, CLI — не runtime-код. |
| II. At-least-once (NON-NEGOTIABLE) | ✅ | Апдейт переживает краш любого процесса: 200 Telegram'у только после COMMIT INSERT в журнал (FR-002); lease + attempts + реклейм по истечении — та же механика, что у исходящих; ack атомарен (`delivered` + `delivered_at` одним UPDATE); краш между POST внешнему серверу и ack закрыт lease-реклеймом — редкий дубль доставки документирован (at-least-once, не exactly-once); UNIQUE-индекса на (bot_slug, update_id) нет, ретраи Telegram поглощаются мягким дедупом на входе по ключу (бот, update_id, payload) — R13; дубли остаются только в неустранимом окне POST/ack, дедуп доставки — скользящим окном на стороне потребителя. Запрос вебхука никогда не ждёт ни внешнего сервера, ни воркера. |
| III. No plaintext secrets | ✅ | Секрет вебхука генерируется системой (`secrets.token_urlsafe(32)`), один раз уходит Telegram'у в `set_webhook` и хранится в БД только как HMAC-SHA256 от значения под выделенным `GRAMMQ_WEBHOOK_MASTER_KEY` (SecretStr, hex 32 байта, отдельный от токен-ключа — «shared by no other purpose»); проверка — `hmac.compare_digest` по дайджестам, plaintext для проверки не нужен (FR-008). Секрет не печатается CLI, не логируется, в ответах отсутствует. Auth-значение доставки — `SecretStr`. Потеря мастер-ключа = 401 на все вебхуки до перерегистрации (recovery повторной `webhook set` — аналог пере-выпуска токенов 006). |
| IV. Two realms (v3.0.0) | ✅ (+MINOR-правка) | `/webhooks/{bot_slug}` — не `/api/v1/*` и не `/api/dashboard/*`: вызывающий — сам Telegram, ни X-API-Key, ни JWT; собственная аутентификация пер-ботовым секретом. Существующий текст IV не запрещает третье пространство (прецедент — `/bootstrap/*` из 005/006), но постоянный (не оконный) публичный surface стоит зафиксировать явно: задача implement — `$speckit-constitution` MINOR v3.1.0 «Telegram webhooks: POST /webhooks/{bot_slug}, header X-Telegram-Bot-Api-Secret-Token, HMAC-SHA256 check value, no API key, no JWT». |
| Asynchrony | ✅ | Хендлеры и воркер — async; httpx.AsyncClient (async, typed); SELECT/INSERT — SQLAlchemy async; HMAC/`compare_digest` — микросекундный CPU-bound без сетевого IO (прецедент: AESGCM в 006); никаких `requests`/`time.sleep`. |
| Type checking | ✅ | mypy --strict на всём новом коде; гейт `uv run check`. |
| Database migrations | ✅ | Одна ревизия Alembic `0004`: создание `gmq_inbound_updates` + `gmq_inbound_claim_state` (с учётом `GRAMMQ_TABLE_PREFIX` через `table_name()`), `ALTER TABLE gmq_bots ADD COLUMN webhook_secret_hmac`. Схема меняется только через миграцию; `alembic upgrade head` на старте контейнера уже выполняется. |
| Testing | ✅ | Контрактные тесты порта — на InMemoryInboundBroker без инфраструктуры + на PostgresInboundBroker при `GRAMMQ_TEST_DATABASE_URL` (иначе skip); unit: эндпоинт (200/401/404/422/503, отсутствие артефактов при отказах, отсутствие секрета в ответах/логах), маппинг ошибок доставки (retry/429/permanent/max-attempts), `webhook_store` (HMAC-двойка, подмена, constant-time), CLI (fake Telegram, порядок Telegram→БД); интеграционные: миграция + жизненный цикл в PG; секретов и deploy-env в тестах нет; покрытие ≥ 80%. |
| Dependency licenses | ✅ | `httpx` — BSD-3-Clause, в разрешённом списке; переезжает из dev-группы в runtime; остальное — stdlib `hmac`/`hashlib` и существующие зависимости. |
| Configuration | ✅ | Шесть новых полей `Settings` через `GRAMMQ_`-канал: `webhook_public_url: str \| None = None` (базовый публичный адрес, не секрет), `webhook_master_key: SecretStr \| None = None`, `inbound_delivery_url: str \| None = None` (не секрет; креды — отдельным заголовком), `inbound_delivery_auth_header_name: str = "Authorization"`, `inbound_delivery_auth_header_value: SecretStr \| None = None`, `inbound_delivery_timeout_seconds: float = 10.0`. Отсутствие `webhook_master_key`/`webhook_public_url` законно на исходящем деплое: CLI `webhook set` отказывает с exit 2, эндпоинт при NULL-значении у бота отвечает 404; inbound-worker при незаданном `inbound_delivery_url` падает на старте (fail-fast своего процесса). Воркеры делят poll/backoff/max_attempts с исходящими (один env-файл). |
| Stack and deployment (v1) | ✅ | Стек не меняется (httpx — та же экосистема, что и в тестах); `run_all.py` порождает третий сервис условно — по наличию `GRAMMQ_INBOUND_DELIVERY_URL` (deploy-артефакт, не runtime-код: читает указатель, не секреты); systemd — новый юнит `gram-mq-inbound-worker.service`; `bot_slug` остаётся единым идентификатором бота (путь вебхука, журнал, файлы). |

Нарушений нет — Complexity Tracking пуст. Третий процесс — не нарушение,
а требование спеки («другой воркер», US2): приём (api) и доставка
(входящий воркер) не блокируют друг друга, исходящий воркер не тормозится
лежащим внешним сервером.

**Re-check после Phase 1 (post-design, обновлено сессией 2026-11-21
(3))**: дизайн-артефакты (research R1–R13, data-model, contracts,
quickstart) сверены с таблицей — статусы не изменились, все ✅. Ключевые
сверки: (1) контракт вебхука — 200 только
после COMMIT, 503 при сбое хранилища — Telegram повторяет (II);
(2) HMAC-дайджест вместо Argon2 на горячем пути — и секретность (III),
и SC-002 ≤ 1 c (Asynchrony/Performance); (3) отсутствие
UNIQUE(bot_slug, update_id) при мягком дедупе со сверкой payload (R13) —
коллизии после простоя не теряются, а гонки лишь просачивают безвредный
дубль (II); (4) `webhook_secret_hmac` в `gmq_bots` умирает вместе со строкой
при ревоке, журнал и доставка переживают (II, edge спеки); (5) httpx
BSD-3 — Dependency licenses. Гейт пройден, ERROR нет.

## Project Structure

### Documentation (this feature)

```text
specs/007-inbound-webhook-updates/
├── plan.md                       # This file ($speckit-plan command output)
├── research.md                   # Phase 0 output ($speckit-plan command)
├── data-model.md                 # Phase 1 output ($speckit-plan command)
├── quickstart.md                 # Phase 1 output ($speckit-plan command)
├── contracts/
│   ├── webhook-endpoint.md       # POST /webhooks/{bot_slug}: аутентификация, коды, тайминг
│   ├── inbound-broker.md         # InboundBrokerPort: семантика очереди входящих
│   ├── update-deliverer.md       # UpdateDeliverer + httpx-адаптер: таксономия ошибок
│   └── webhook-admin-cli.md      # gram-mq webhook set/delete: порядок операций, exit-коды
└── tasks.md                      # Phase 2 output ($speckit-tasks command)
```

### Source Code (repository root)

```text
src/gram_mq/
├── ports/
│   ├── inbound.py            # NEW: InboundBrokerPort, InboundUpdate, InboundDelivery
│   │                         #      (UnknownBotError/IllegalDeliveryStateError — из ports/broker.py)
│   └── deliverer.py          # NEW: UpdateDeliverer, DeliveryError(retry_after, permanent)
├── models/
│   └── inbound.py            # NEW: InboundUpdateRow (журнал), InboundStatus
│                             #      (queued/delivering/delivered/failed), BotInboundClaimState
├── adapters/
│   ├── memory/inbound_broker.py   # NEW: in-memory дубль порта (тесты)
│   ├── postgres/inbound_broker.py # NEW: журнал-как-очередь, round-robin по ботам
│   ├── postgres/webhook_auth.py   # NEW: SqlAlchemyWebhookAuth: SELECT бота + constant-time HMAC
│   └── http/deliverer.py          # NEW: HttpxUpdateDeliverer + classify() (чистый маппинг)
├── api/
│   ├── app.py                # + WebhookAuth-протокол, POST /webhooks/{bot_slug}
│   │                         #      (маршрут регистрируется только при инъекции портов)
│   └── main.py               # + сборка: PostgresInboundBroker + SqlAlchemyWebhookAuth
├── inbound_worker/
│   ├── __init__.py           # NEW package: третий процесс
│   ├── __main__.py
│   ├── runner.py             # claim → deliver → атомарный ack; 429 — без счёта попытки;
│   │                         # backoff = base * 2**attempts, cap 300s (как worker/runner.py)
│   └── main.py               # сборка: broker + HttpxUpdateDeliverer; fail-fast без delivery_url
├── webhook_store.py          # NEW: чистый модуль — генерация секрета, HMAC-SHA256,
│                             #      constant-time сверка, парсинг мастер-ключа (по образцу token_store)
├── settings.py               # + 6 полей (см. Configuration)
├── models/bots.py            # + webhook_secret_hmac: String(64) | None
└── cli/
    ├── main.py               # + subcommand webhook (set/delete)
    └── webhook.py            # NEW: сборка токена (SplitFileBotConfig), set_webhook через aiogram,
                              #      порядок Telegram→БД, exit-коды по конвенции 0/2/4

alembic/versions/20261121_0004_inbound_updates.py   # NEW (см. data-model.md)

deploy/
├── run_all.py                # третий сервис условно: spawn при GRAMMQ_INBOUND_DELIVERY_URL
└── gram-mq-inbound-worker.service   # NEW: юнит-близнец gram-mq-worker.service

.env.example                  # + 6 переменных с комментариями-окнами
README.md                     # + секция inbound-потока и команд

tests/
├── contract/test_inbound_broker_port.py   # NEW: контракт порта (memory + postgres-gated)
├── unit/
│   ├── test_webhook_endpoint.py           # NEW: коды/артефакты/секреты-в-логах
│   ├── test_inbound_runner.py             # NEW: маппинг ошибок доставки
│   ├── test_webhook_store.py              # NEW: HMAC/подмена/constant-time
│   └── test_webhook_cli.py                # NEW: порядок операций, exit-коды (fake Telegram)
└── integration/test_inbound_db.py         # NEW: миграция 0004, жизненный цикл в PG
```

**Structure Decision**: расширение существующей структуры без новых
пакетов, кроме `inbound_worker/` — отдельный процесс по требованию спеки
(US2), зеркалит `worker/` по составу (`runner` + сборка + `__main__`).
Входящая очередь — сиблинг-порт `ports/inbound.py` вместо обобщения
`BrokerPort`: контракт фичи 001 исходящий (`chat_id`,
`telegram_message_id` в ack), его пере-обобщение — переписывание
проверенного контракта; дублирование SQL-формы claim в
`PostgresInboundBroker` сознательное и явное (разные модели/таблицы).
Секрет вебхука — чистым модулем `webhook_store.py` (образец —
`token_store.py` из 006): и API (проверка), и CLI (генерация + передача)
зависят от него, а не друг от друга.
