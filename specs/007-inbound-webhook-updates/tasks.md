# Tasks: Приём вебхуков Telegram и доставка апдейтов внешнему сервису

**Input**: Design documents from `/specs/007-inbound-webhook-updates/`

**Prerequisites**: plan.md, spec.md, research.md (R1–R13), data-model.md,
contracts/webhook-endpoint.md, contracts/inbound-broker.md,
contracts/update-deliverer.md, contracts/webhook-admin-cli.md, quickstart.md

**Tests**: включены — конституция (Engineering Standards / Testing)
обязывает покрывать каждую публичную функцию при покрытии ≥ 80%.

**Organization**: задачи сгруппированы по user story; каждая фаза —
независимо проверяемый инкремент. Формат: `[ID] [P?] [Story] Описание с
путём к файлу`.

## Path Conventions

Single project: `src/`, `tests/`, `alembic/`, `deploy/` в корне
репозитория (см. plan.md → Project Structure).

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 Перенести `httpx` из dev-зависимостей в runtime в
  `pyproject.toml` (`dependencies`), выполнить `uv sync` (обновить
  `uv.lock`). Лицензия BSD-3-Clause — разрешённый список (research R6);
  тесты уже используют httpx (ASGITransport), конфликтов нет.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Цель**: порты, настройки, секретное ядро, схема БД и оба адаптера
очереди — всё, что нужно каждой истории.

- [x] T002 [P] Добавить поля в `src/gram_mq/settings.py` (data-model →
  Настройки; канал `GRAMMQ_`, существующий SettingsConfigDict):
  `webhook_public_url: str | None = None`,
  `webhook_master_key: SecretStr | None = None`,
  `inbound_delivery_url: str | None = None`,
  `inbound_delivery_auth_header_name: str = "Authorization"`,
  `inbound_delivery_auth_header_value: SecretStr | None = None`,
  `inbound_delivery_timeout_seconds: float = 10.0`. Тем же коммитом
  обновить `.env.example`: шесть переменных с окнами применения по
  образцу bootstrapping-секций (мастер-ключ — hex 32 байт, потеря = 401
  всех вебхуков до перерегистрации; delivery_url — условие третьего
  сервиса в run_all).
- [x] T003 [P] Создать порты: `src/gram_mq/ports/inbound.py` —
  `InboundUpdate(id: UUID, bot_slug: str, update_id: int, payload:
  dict)`, `InboundDelivery(…, context: dict)`,
  `InboundBrokerPort` (`enqueue_inbound` / `claim` / `ack` / `retry` /
  `dead_letter` / `queue_depth`; ошибки `UnknownBotError`,
  `IllegalDeliveryStateError` — импорт из `ports/broker.py`; семантика —
  contracts/inbound-broker.md, включая идемпотентность `enqueue_inbound`
  по (bot_slug, update_id, payload)); `src/gram_mq/ports/deliverer.py` —
  `DeliveryError(reason, *, retry_after: float | None = None,
  permanent: bool = False)` и `UpdateDeliverer.deliver_update(*, update:
  dict) -> None` (contracts/update-deliverer.md). Docstring-контракты в
  стиле существующих портов.
- [x] T004 [P] Создать чистый модуль `src/gram_mq/webhook_store.py`
  (research R2, contracts/webhook-admin-cli.md): `make_webhook_secret()
  -> str` (`secrets.token_urlsafe(32)`); `webhook_secret_hmac(master_key:
  bytes, secret: str) -> str` — hex HMAC-SHA256; `verify_webhook_secret(
  master_key: bytes, stored_hex: str, presented: str | None) -> bool` —
  `hmac.compare_digest` по дайджестам, `presented is None` → False без
  вычислений; `parse_webhook_master_key(SecretStr | None) -> bytes |
  None` по образцу `token_store.parse_master_key` (hex 64 симв. → 32
  байта, иначе None). Тесты `tests/unit/test_webhook_store.py`: roundtrip
  HMAC; неверный presented → False; None → False; битый stored_hex →
  False; секрет не появляется в repr/исключениях.
- [x] T005 Создать `src/gram_mq/models/inbound.py` (data-model):
  `InboundStatus` enum (`queued/delivering/delivered/failed`, native
  enum `table_name("inbound_status")`), `InboundUpdateRow` над
  `table_name("inbound_updates")` — поля и NULL-ность дословно из
  data-model (`id` UUID PK, `bot_slug` String(64) NOT NULL без FK,
  `update_id` BigInteger NOT NULL, `payload` JSONB NOT NULL, `status`,
  `available_at` NOT NULL, `locked_at/locked_by/lease_expires_at`
  nullable, `attempts/max_attempts` Integer NOT NULL, `error` Text NULL,
  `created_at` NOT NULL server_default now(), `delivered_at` NULL);
  `BotInboundClaimState` над `table_name("bot_inbound_claim_state")`
  (`bot_slug` PK, `last_claim_at` nullable). Партициальные индексы
  claim/reclaim/depth + НЕуникальный `dupe (bot_slug, update_id)`.
  Дописать `webhook_secret_hmac: Mapped[str | None] = String(64)` в
  `src/gram_mq/models/bots.py`. Миграция
  `alembic/versions/20261121_0004_inbound_updates.py`: создание обеих
  таблиц с индексами + `ALTER TABLE gmq_bots ADD COLUMN
  webhook_secret_hmac VARCHAR(64) NULL`; downgrade в обратном порядке.
- [x] T006 Реализовать `src/gram_mq/adapters/memory/inbound_broker.py` —
  `InMemoryInboundBroker` (дубль порта из T003): зеркало
  `adapters/memory/broker.py` — статусы строками, injectable
  `MemoryStore` (рестарт) и часы, справедливость round-robin, lease по
  часам, идемпотентность `enqueue_inbound` по (bot_slug, update_id,
  payload) — точное совпадение поглощается без записи (R13).
- [x] T007 Реализовать `src/gram_mq/adapters/postgres/inbound_broker.py`
  — `PostgresInboundBroker` по образцу `postgres/broker.py`:
  `enqueue_inbound` — единый SELECT существования бота + точного
  совпадения записи (`payload` JSONB-равенство, поиск по индексу dupe);
  найдено совпадение → поглощение (no-op), бот отсутствует →
  `UnknownBotError`, иначе INSERT `queued/available_at=now()/attempts=0/
  max_attempts=снапшот настройки`; `claim` — двухшаговый round-robin по
  `gmq_bot_inbound_claim_state` c `FOR UPDATE SKIP LOCKED` и реклеймом
  истёкших lease; `ack` — атомарный UPDATE `delivered` + `delivered_at`
  одним оператором; `retry` / `dead_letter` / `queue_depth` / `_transition`
  — зеркально исходящему брокеру.
- [x] T008 Контрактные тесты `tests/contract/test_inbound_broker_port.py`
  по образцу `test_broker_port.py`: параметризация InMemoryInboundBroker
  (всегда) + PostgresInboundBroker (при `GRAMMQ_TEST_DATABASE_URL`,
  иначе skip). Покрыть: UnknownBotError; поглощение точного повтора
  ( bot+update_id+payload → no-op) и приём иного payload при том же
  update_id как новой записи (FR-012); claim/lease/реклейм по истечении;
  retry со сдвигом available_at и `count_attempt=False` (429-пауза);
  dead_letter; исчерпание max_attempts; справедливость между ботами;
  queue_depth.

**Checkpoint**: порты + очередь обоих вкусов зелёные; истории можно
делать параллельно.

---

## Phase 3: User Story 1 - Приём апдейта от Telegram без потерь (Priority: P1) 🎯 MVP

**Goal**: `POST /webhooks/{bot_slug}` с проверкой секрета и мягким
дедупом, 200 строго после COMMIT (FR-001/002/008/009/012).

**Independent Test**: quickstart.md §1 шаги 4–5 — curl с корректным
секретом → 200 и строка `queued` в `gmq_inbound_updates`; без секрета →
401; неизвестный бот → 404; без `update_id` → 422; повтор байт-в-байт →
200 без новой строки.

### Implementation for User Story 1

- [x] T009 [US1] `src/gram_mq/adapters/postgres/webhook_auth.py` —
  `SqlAlchemyWebhookAuth`: SELECT `bot_slug, webhook_secret_hmac` из
  `gmq_bots`; результат-тип `ok / unknown / rejected`: бот отсутствует
  ИЛИ `webhook_secret_hmac IS NULL` → `unknown` (неразличимо, без
  оракула); иначе `verify_webhook_secret` (T004) → `ok`/`rejected`.
- [x] T010 [US1] Маршрут `POST /webhooks/{bot_slug}` в
  `src/gram_mq/api/app.py` (contracts/webhook-endpoint.md): регистрация
  только при инъекции `inbound_broker` + `webhook_auth` (иначе весь
  `/webhooks/*` — 404, обратная совместимость); тело — произвольный
  JSON-объект, обязательное поле только `update_id: int` (иначе 422 без
  артефактов); коды: 200 после COMMIT или поглощения дубля, 401
  (нет/неверный заголовок `X-Telegram-Bot-Api-Secret-Token` — единый,
  без различения), 404 (`unknown`), 422, 503 при сбое фиксации
  (транзакция абортирована). Логирование: id/bot/update_id + факт
  поглощения; payload никогда не логируется.
- [x] T011 [P] [US1] Тесты `tests/unit/test_webhook_endpoint.py` (httpx
  ASGITransport + InMemoryInboundBroker + fake auth): все коды и их
  порядок проверок; поглощение ретрая (200, записей не прибавилось);
  коллизия update_id с иным payload → новая запись; 401/404/422 не
  создают артефактов; сбой enqueue → 503; маршрут отсутствует (404 на
  всём `/webhooks/*`), когда порты не инъецированы; секрет не встречается
  в ответах и логах.
- [x] T012 [US1] Сборка `src/gram_mq/api/main.py`: инъекция
  `PostgresInboundBroker` + `SqlAlchemyWebhookAuth` (мастер-ключ —
  `parse_webhook_master_key(settings.webhook_master_key)`; None →
  вебхук-порты не инъецируются, деплой без фичи не меняется).
- [x] T013 [US1] Интеграционные тесты `tests/integration/test_inbound_db.py`
  (только при `GRAMMQ_TEST_DATABASE_URL`): миграция 0004 накатывается на
  чистой БД (таблицы/индексы/колонка bots); постановка через
  PostgresInboundBroker с реальной JSONB-семантикой дедупа (поглощение
  точного повтора, приём иного payload); ревок бота → enqueue →
  UnknownBotError, существующие строки журнала остаются.

**Checkpoint**: US1 работает самостоятельно — апдейт от «Telegram»
надёжно лежит в журнале.

---

## Phase 4: User Story 2 - Доставка апдейтов внешнему сервису (Priority: P1)

**Goal**: третий процесс `gram-mq-inbound-worker` доставляет payload без
изменений на внешний эндпоинт, at-least-once с экспоненциальным backoff
(FR-003/004/005/006/011/013).

**Independent Test**: quickstart.md §1 шаг 6 + таблица §2 — положить
строку вручную, поднять приёмник, запустить воркер: строка → delivered,
payload у приёмника идентичен; погасить приёмник — retry с растущей
задержкой, после max_attempts → failed.

### Implementation for User Story 2

- [x] T014 [P] [US2] `src/gram_mq/adapters/http/deliverer.py` —
  `HttpxUpdateDeliverer` + чистая `classify()` (contracts/
  update-deliverer.md): 2xx → успех; 429 (+Retry-After, если разобран) →
  `retry_after`; таймаут/обрыв/5xx → повторимо; прочие 4xx →
  `permanent=True`. Постоянный `httpx.AsyncClient`, POST JSON на
  `settings.inbound_delivery_url`, заголовок авторизации только при
  заданном `inbound_delivery_auth_header_value` (имя —
  `…_header_name`), таймаут `…_timeout_seconds`; значение заголовка
  редактируется из `reason`/логов; `aclose()` на остановке.
- [x] T015 [US2] `src/gram_mq/inbound_worker/` пакет: `runner.py` —
  зеркало `worker/runner.py`: claim → deliver_update → атомарный ack;
  `retry_after` → retry БЕЗ счёта попытки; повторимая ошибка →
  `min(base * 2**attempts, 300)` со счётом; исчерпание → dead_letter
  `max attempts exceeded: …`; `permanent` → dead_letter сразу;
  `main.py` — fail-fast: нет `inbound_delivery_url` → ясная ошибка и
  выход на старте; сборка PostgresInboundBroker + HttpxUpdateDeliverer
  (poll/backoff/max_attempts — общие настройки воркеров);
  `__init__.py`/`__main__.py`; консольный скрипт
  `gram-mq-inbound-worker = gram_mq.inbound_worker.main:main` в
  `pyproject.toml`.
- [x] T016 [US2] Тесты `tests/unit/test_inbound_runner.py`: fake-брокер +
  fake `UpdateDeliverer` — маппинг всех исходов (успех → ack;
  retry_after → retry без счёта; повторимая → backoff-расписание с
  капом 300; permanent → dead_letter; max_attempts → failed);
  `classify()` и `HttpxUpdateDeliverer` через `httpx.MockTransport`
  (без сети): все классы ответов, наличие/отсутствие заголовка
  авторизации, секрет не в reason.
- [x] T017 [US2] Деплой: `deploy/run_all.py` — третий сервис
  `gram_mq.inbound_worker` порождается только при наличии
  `GRAMMQ_INBOUND_DELIVERY_URL` в окружении (deploy-артефакт читает
  указатель-переменную, не секрет; сигнальная логика «один умер —
  гасим всех» сохраняется); `deploy/gram-mq-inbound-worker.service` —
  близнец `gram-mq-worker.service`. Dockerfile не меняется.

**Checkpoint**: сквозной поток US1+US2 — апдейт от вебхука доходит до
внешнего сервера и переживает его недоступность.

---

## Phase 5: User Story 3 - Регистрация вебхука бота (Priority: P2)

**Goal**: `gram-mq webhook set|delete` (FR-007/008): секрет генерирует
система, оператор его не видит.

**Independent Test**: quickstart §1 шаг 3 (или его fake-аналог в
тестах): после `webhook set` у Telegram активен вебхук, в `gmq_bots`
появился HMAC; после `webhook delete` — колонка NULL, приём 404.

### Implementation for User Story 3

- [x] T018 [US3] `src/gram_mq/cli/webhook.py` + subcommand в
  `src/gram_mq/cli/main.py` (contracts/webhook-admin-cli.md):
  `webhook set <bot_slug>` — предусловия (бот читается обеими половинами
  через `SplitFileBotConfig`; заданы `webhook_public_url` и валидный
  `webhook_master_key`) иначе exit 2; генерация секрета; `set_webhook(
  url=f"{public}/webhooks/{slug}", secret_token=secret)` через aiogram
  `Bot` от собранного токена; после подтверждения — запись
  `webhook_secret_hmac` (сбой записи → exit 4: Telegram уже знает новый
  секрет, безопасное направление отказа). `webhook delete <bot_slug>` —
  `delete_webhook()` → колонка NULL (сбой записи → exit 4). Вывод: slug,
  полный адрес, статус; секрет не печатается и не логируется. Exit-коды
  по конвенции 0/2/4.
- [x] T019 [P] [US3] Тесты `tests/unit/test_webhook_cli.py`: fake
  Telegram-клиент (инъектируемый) — порядок операций «Telegram → БД»
  (запись HMAC только после подтверждения set_webhook); сбой записи БД →
  exit 4; предусловия → exit 2 без побочных эффектов; delete → NULL;
  секрет отсутствует в stdout/логах.

**Checkpoint**: полный жизненный цикл приёма управляется CLI.

---

## Phase 6: User Story 4 - Наблюдаемость потока входящих (Priority: P3)

**Goal**: журнал даёт оператору глубину, счётчики, возраст старейшей и
детали failed (FR-010); логи коррелированы по update_id.

**Independent Test**: quickstart §2 «Мониторинг»: при живом потоке и при
лежащем приёмнике SQL из README возвращает осмысленные глубину/возраст;
в логах события апдейта связываются по update_id.

### Implementation for User Story 4

- [x] T020 [US4] README.md — секция inbound-потока, включая контракт
  приёмной стороны (FR-013): SQL-запросы по
  `gmq_inbound_updates` (количество по статусам, глубина
  `queued+delivering`, возраст старейшей незавершённой записи
  `now() - min(created_at)` по незавершённым, последние failed с
  attempts/error); сверить, что логи приёма/поглощения/попыток доставки/
  исхода во всех компонентах (T010, T015) несут update_id — при
  отсутствии довести.

**Checkpoint**: эксплуатация потока не требует доступа к коду.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T021 Правка конституции через `$speckit-constitution`: MINOR
  v3.1.0 — Principle IV дополняется постоянным третьим URL-пространством
  Telegram-вебхуков (`POST /webhooks/{bot_slug}`, заголовок
  `X-Telegram-Bot-Api-Secret-Token`, HMAC-SHA256 проверочное значение,
  без X-API-Key и JWT) — зафиксировано в plan.md → Constitution Check
  (IV) по прецеденту правки v3.0.0 фичей 006.
- [x] T022 [P] Прогнать quickstart.md целиком (гейты §0 + ручные §1–2,
  включая новые сценарии «Ретрай Telegram» и «Коллизия update_id») и
  зафиксировать результат в README/заметке фичи.
- [x] T023 Финальные гейты: `uv run check` (ruff + mypy --strict),
  `uv run test`, `coverage report --fail-under=80`; при DB-gated
  пропусках — прогон с `./.env.test` по возможности.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: без зависимостей.
- **Foundational (Phase 2)**: после Phase 1; T002/T003/T004/T005
  параллельны, T006 ← T003, T007 ← T003+T005, T008 ← T006+T007.
- **User Stories (Phases 3–6)**: каждая стартует после Phase 2; US1 и
  US2 — параллельно (US2 тестируется ручной постановкой строки, без US1);
  US3 зависит от T004/T005 (секрет + колонка), не от US1/US2; US4 —
  последней (документирует работающий поток).
- **Polish (Phase 7)**: после всех историй.

### User Story Dependencies

- **US1**: Phase 2 (T003, T005–T008).
- **US2**: Phase 2; от US1 не зависит (независимый тест — ручная
  постановка в очередь).
- **US3**: T004, T005 (колонка), T009 не нужен.
- **US4**: работающие US1+US2 для текстов README.

### Parallel Opportunities

- Phase 2: T002 ∥ T003 ∥ T004 ∥ T005; далее T006 ∥ (T007 после T005).
- Истории: US1 (T009–T013) ∥ US2 (T014–T017) ∥ US3 (T018–T019) после
  Phase 2.
- Внутри US1: T009 ∥ T010 (разные файлы); тесты T011 параллельны сборке
  T012.

---

## Parallel Example: User Story 1

```bash
# Независимые задачи US1 (разные файлы):
Task: "T009 SqlAlchemyWebhookAuth в src/gram_mq/adapters/postgres/webhook_auth.py"
Task: "T010 Маршрут POST /webhooks/{bot_slug} в src/gram_mq/api/app.py"

# Затем связка и тесты:
Task: "T011 Тесты tests/unit/test_webhook_endpoint.py"
Task: "T012 Сборка src/gram_mq/api/main.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1–2 (setup + порты/очередь) → 2. Phase 3 (US1) → 3. STOP:
   приём надёжен сам по себе (Telegram → журнал), quickstart §1 шаги
   4–5 зелёные.

### Incremental Delivery

1. Setup + Foundational → очередь входящих обоих вкусов с контрактами.
2. +US1 → надёжный приём (MVP).
3. +US2 → сквозной поток с ретраями — основная ценность фичи.
4. +US3 → управление жизненным циклом из CLI.
5. +US4 → эксплуатация; Phase 7 — конституция, quickstart, гейты.

---

## Notes

- [P] = разные файлы, нет зависимостей от незавершённых задач.
- Тесты обязательны (конституция); DB-гейтированные — skip без
  `GRAMMQ_TEST_DATABASE_URL`, это норма проекта, не пропуск.
- Секрет вебхука (plaintext) существует только в памяти CLI в окно
  `webhook set` — нигде больше (конституция III, FR-008).
- Имя заголовка `X-Telegram-Bot-Api-Secret-Token` — внешний контракт
  Telegram; статусы/имена проекта не переименовывать под внешние
  рекомендации (сессия спеки 2026-11-21 (3)).
- Ревью всего фичи целиком до коммита; коммит — один после одобрения.
