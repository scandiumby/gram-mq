---
description: "Task list: HTTP API приёма сообщений и доставка в Telegram (MVP)"
---

# Tasks: HTTP API приёма сообщений и доставка в Telegram (MVP)

**Input**: Design documents from `/specs/003-messaging-api-worker/`

**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/{http-api,telegram-sender,bot-config}.md, quickstart.md

**Tests**: Включены — конституция (Testing) требует покрытия публичных функций, фейкового TelegramSender и быстрых тестов хэширования ключей; спека требует SC-001–SC-005. Порядок: тесты раньше реализации, тесты обязаны падать до имплементации.

**Organization**: Задачи сгруппированы по user stories спеки (US1 ключи → US2 API → US3 воркер → US4 подсчёт); US2 зависит от US1 (нужен ключ для интеграционной проверки), US3 независимо по коду.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Можно параллельно (разные файлы, нет зависимостей от незакрытых задач)
- **[Story]**: Принадлежность к user story спеки
- Точные пути файлов обязательны в описании

## Path Conventions

Single project: `src/`, `tests/`, `deploy/` от корня репозитория (см. plan.md → Project Structure).

## Phase 1: Setup

**Purpose**: Зависимости, точки входа, скелеты пакетов.

- [x] T001 В pyproject.toml: dependencies += fastapi, uvicorn, argon2-cffi, aiogram>=3; dev += httpx; [project.scripts] += `gram-mq-api = "gram_mq.api.main:main"`, `gram-mq-worker = "gram_mq.worker.main:main"` (без прямых ссылок на `__main__`-модули); выполнить `uv sync --group dev` (лицензии проверены в plan.md Constitution Check / research R9)
- [x] T002 [P] Скелеты: src/gram_mq/api/__init__.py, src/gram_mq/worker/__init__.py, src/gram_mq/adapters/telegram/__init__.py, каталог deploy/

**Checkpoint**: `uv run check` зелёный на пустых пакетах

---

## Phase 2: Foundational (порты, схема, генерация/верификация ключа, адаптер aiogram)

**Purpose**: Контракты и инфраструктура, на которые опираются все stories. Блокирует US1–US4.

- [x] T003 [P] src/gram_mq/ports/telegram.py: `TelegramSender` (Protocol, `send_text(*, bot_token, chat_id, text) -> str`) и `TelegramSendError(reason: str, *, retry_after: float | None = None, permanent: bool = False)` — таксономия по contracts/telegram-sender.md
- [x] T004 [P] src/gram_mq/models/api_keys.py: `ApiKey` — prefix: String(12) PK (первые 12 символов ключа вкл. `gmq_`), key_hash: String(128) NOT NULL (Argon2id-encoded), created_at: timestamptz NOT NULL server_default now(), revoked_at: timestamptz NULL; зарегистрировать в общем импорте моделей (как bots/messages в tests/conftest.py)
- [x] T005 Alembic-ревизия `add gmq_api_keys` (alembic revision --autogenerate + правка вручную при необходимости; только CREATE TABLE gmq_api_keys)
- [x] T006 src/gram_mq/api/auth.py: `generate_api_key() -> tuple[str, str]` (ключ `gmq_` + token_urlsafe(43-симв. base64url), prefix = ключ[:12]), `hash_key(key) -> str` (argon2-cffi PasswordHasher, Argon2id), `ApiKeyVerifier` — принимает инжектируемые `fetch(prefix) -> tuple[str, bool] | None` (хэш, отозван) и hasher; `verify(key) -> bool`: формат → prefix → revoked → Argon2id-проверка через `asyncio.to_thread` (комментарий-обоснование: CPU-bound, конституция Asynchrony); неверный формат/неизвестный prefix/не сошёлся/отозван → False
- [x] T007 [P] src/gram_mq/adapters/telegram/aiogram_sender.py: `AiogramTelegramSender` — кэш aiogram.Bot по токену, `send_text` → `bot.send_message(chat_id=..., text=...)` без parse_mode, возврат `str(message.message_id)`; чистая функция `classify(exc) -> TelegramSendError` по contracts/telegram-sender.md (RetryAfter → retry_after, NetworkError/timeout → retryable, BadRequest/Forbidden/прочие API-ошибки → permanent); `aclose()` закрывает ботов
- [x] T008 Settings (src/gram_mq/settings.py): += api_host="127.0.0.1", api_port=8080, worker_poll_interval=1.0, worker_backoff_base_seconds=5.0 (не секреты, versioned defaults)

**Checkpoint**: юнит-тесты порта/верификатора/адаптера зелёные (T009–T011)

- [x] T009 [P] RED+GREEN: tests/unit/test_auth.py — генерация: формат `^gmq_[A-Za-z0-9_-]{43}$`, ключ != prefix, 100 ключей попарно различны; hash: Argon2id-хэш ≠ ключ, verifier.verify сходится на своём хэше; verifier: неверный формат → False, неизвестный prefix → False, чужой хэш → False, отозванный → False (дешёвый hasher t=1/m=64KiB для скорости, прод-дефолты не трогаем)
- [x] T010 [P] RED+GREEN: tests/unit/test_aiogram_sender.py — classify() на исключениях aiogram без сети (TelegramRetryAfter(retry_after=7) → retry_after=7/permanent=False; TelegramNetworkError → retryable; TelegramBadRequest → permanent; TelegramForbidden → permanent; ValueError → permanent)
- [x] T011 [P] RED+GREEN: tests/unit/test_bot_config.py — FileBotConfig (src/gram_mq/worker/bot_config.py: порт `BotConfig` `token_for(slug) -> str | None` + адаптер `FileBotConfig(bots_dir)` поверх gram_mq.cli.token.read_token_file): валидный файл → `{id}:{secret}`; отсутствует → None; повреждённый → None

---

## Phase 3: User Story 1 - Выдача API-ключа (Priority: P1) 🎯 MVP

**Goal**: `gram-mq key add` печатает ключ один раз; в БД prefix + Argon2id-хэш.

**Independent Test**: quickstart.md §2 — вывод ключа, проверка таблицы gmq_api_keys (prefix+hash, без ключа).

### Tests for User Story 1

- [x] T012 [P] [US1] RED: tests/unit/test_keys_cli.py — key add с фейковой вставкой: напечатан ровно один раз и только в stdout; в «БД» (fake) prefix и Argon2id-хэш, ключа нет; verifier.verify(напечатанный ключ) → True; повторный запуск не повторяет старый ключ (уникальность)

### Implementation for User Story 1

- [x] T013 [US1] src/gram_mq/cli/keys.py: `run_key_add(*, insert: Callable[[str, str], Awaitable[None]]) -> tuple[str, str]` (generate → hash → insert(prefix, hash) → вернуть (ключ, prefix) для печати); подключение в src/gram_mq/cli/main.py: подкоманда `key add` (сборка insert через db.get_session_factory + модель ApiKey), вывод «ключ показывается один раз»; exit-коды по аналогии с bot add (0/2/4)
- [x] T014 [US1] GREEN: `uv run pytest tests/unit -q`

**Checkpoint**: ключи выдаются; таблица наполняется

---

## Phase 4: User Story 2 - Постановка сообщения (Priority: P1) 🎯 MVP

**Goal**: POST /api/v1/messages: X-API-Key → 202 + id; 401/404/422 по контракту.

**Independent Test**: quickstart.md §3 на memory-брокере — матрица кодов без БД (автотест) и с БД (T022).

### Tests for User Story 2

- [x] T015 [P] [US2] RED: tests/unit/test_api_app.py — create_app с InMemoryBroker(+register_bot) и фейковым verifier (httpx AsyncClient + ASGITransport): 202 + {"id": uuid} и строка в memory-сторе со status queued, payload text (SC-001); 401: без ключа / мусорный / отозванный / ключ в query-параметре — тело ответа одинаковое `{"detail":"unauthorized"}` (FR-002); 404 unknown bot_slug (FR-003, UnknownBotError); 422: пустой bot_slug, пустой text, text 4097 символов, chat_id "abc" и пустой (FR-004); GET /health → 200 без ключа (FR-010); POST /api/v1/bots и PUT /api/v1/messages → 404/405 — HTTP-регистрации нет (SC-005); полный ключ не встречается в ответах

### Implementation for User Story 2

- [x] T016 [US2] src/gram_mq/api/schemas.py: `MessageIn(BaseModel)` — bot_slug: str min1 max64; chat_id: str pattern `^(-?\d{1,18}|@[A-Za-z0-9_]{4,64})$`; text: str min1 max4096 (data-model.md, дословно)
- [x] T017 [US2] src/gram_mq/api/app.py: `create_app(*, broker: BrokerPort, key_verifier) -> FastAPI` — зависимость авторизации (только заголовок X-API-Key, 401 `{"detail":"unauthorized"}` без деталей), POST /api/v1/messages: MessageIn → OutboundMessage(id=uuid4(), bot_slug, chat_id, payload={"text": text}) → broker.enqueue; UnknownBotError → 404 `{"detail":"bot not found"}`; 202 `{"id": str(id)}`; GET /health без авторизации `{"status":"ok"}`; логирование без секретов (research R12)
- [x] T018 [US2] src/gram_mq/api/main.py: main() — сборка PostgresBroker + ApiKeyVerifier на db.get_session_factory, uvicorn.run(build_default_app, factory=True, host=settings.api_host, port=settings.api_port); src/gram_mq/api/__main__.py — тонкая обёртка для python -m
- [x] T019 [US2] GREEN: `uv run pytest tests/unit -q`; ручной smoke: GRAMMQ_DATABASE_URL=... uv run gram-mq-api & → curl /health = 200 (quickstart §3 преамбула)

**Checkpoint**: API ставит сообщения в очередь по контракту

---

## Phase 5: User Story 3 - Доставка воркером (Priority: P1) 🎯 MVP

**Goal**: воркер: claim → send → атомарный ack; retry/429/permanent/max_attempts; at-least-once.

**Independent Test**: quickstart.md §4–§6; автотест SC-003 (разрыв send→ack).

### Tests for User Story 3

- [x] T020 [P] [US3] RED: tests/unit/test_worker.py — run_worker/process_delivery на InMemoryBroker(+FakeClock)+FakeSender+FakeBotConfig: happy → status sent, telegram_message_id записан, sent_at; retryable-ошибка → queued, attempts+1, available_at = now+backoff (по FakeClock); 429 retry_after=30 → задержка ≥30 c даже если backoff меньше (FR-007); permanent → dead_letter: status failed, error содержит reason; token None (нет файла) → retry, после max_attempts → failed; max_attempts на retryable → failed; SC-003: send прошёл, ack не выполнен, lease истёк (FakeClock.advance) → повторный claim доставляет снова (дубль задокументирован) → ack → sent; воркер не инспектирует Delivery кроме payload/context

### Implementation for User Story 3

- [x] T021 [US3] src/gram_mq/worker/runner.py: `process_delivery(delivery, *, broker, sender, bot_config, backoff_base)`: token = bot_config.token_for → None → broker.retry(delay=backoff, reason="bot token file missing or invalid"); send_text → delivery.context["telegram_message_id"] = tg_id → broker.ack; TelegramSendError → permanent ? broker.dead_letter(reason) : broker.retry(delay=max(retry_after или 0, backoff_base * 2**attempts, cap 300), reason); `run_worker(*, broker, sender, bot_config, poll_interval, backoff_base, worker_id)`: цикл claim → обработка; пусто → asyncio.sleep(poll_interval); логи без секретов
- [x] T022 [US3] src/gram_mq/worker/main.py: main() — PostgresBroker(database_url) + AiogramTelegramSender + FileBotConfig(settings.bots_dir) + worker_id=f"{hostname}:{pid}" → asyncio.run(run_worker(...)), graceful shutdown (aclose адаптеров в finally); src/gram_mq/worker/__main__.py — тонкая обёртка для python -m
- [x] T023 [US3] GREEN: `uv run pytest tests/unit -q`

**Checkpoint**: доставка работает end-to-end на фейке

---

## Phase 6: User Story 4 - Подсчёт доставленных (Priority: P2)

**Goal**: SC-002: SQL-подсчёт sent совпадает с числом доставок; интеграция на реальной БД.

### Tests for User Story 4

- [x] T024 [US4] RED: tests/integration/test_api_db.py (DB-gated, skip без TEST_DATABASE_URL, миграции через _pg_schema): TRUNCATE gmq_api_keys/bots/messages; регистрация бота + вставка ключа (hash_key) → POST 202 → строка в gmq_messages status queued (SC-001); 401/404 не меняют count(*); SC-002: N=3 постановок → process_delivery с реальным PostgresBroker + FakeSender → count(*) WHERE status='sent' == 3; SQL-запрос подсчёта — дословно из quickstart.md §5
- [x] T025 [US4] GREEN: `uv run pytest tests/integration -q`

---

## Phase 7: Polish & Cross-Cutting

- [x] T026 deploy/gram-mq-api.service и deploy/gram-mq-worker.service: User=gram-mq, ExecStart=/opt/gram-mq/.venv/bin/gram-mq-api|gram-mq-worker, Restart=on-failure, единственная Environment=GRAMMQ_ENV_FILE=/etc/gram-mq/.env (указатель, без значений — FR-013, research R10)
- [x] T027 Гейты: `uv run check` (ruff+format+mypy --strict) и `uv run coverage run -m pytest && uv run coverage report` ≥ 80%
- [x] T028 Прогнать quickstart.md §0–§5 на тестовой БД (живой бот — опционально, §6 покрыт автотестом SC-003); результат зафиксировать в specs/003-messaging-api-worker/checklists/requirements.md (Notes)

**Checkpoint**: 🎯 **MVP STOP-POINT** — весь MVP-цикл (ключ → POST → доставка → подсчёт) работает; пост-MVP фаза не реализуется

---

## Phase 8: Post-MVP (Tech Debt — НЕ РЕАЛИЗОВЫВАТЬ в этом цикле)

> Отложено сознательно (spec.md → Future Enhancements). `$speckit-implement` не выполняет; консолидировано в specs/004-post-mvp-hardening/spec.md (трассировка: T029 → FR-003/FR-004, T030 → FR-005, T031 → FR-006, T032 → FR-007, T033 → FR-008, T034 → FR-009).

- [ ] T029 [US-Future] CLI управления ключами: `gram-mq key list` (prefix, created_at, revoked_at), `gram-mq key revoke <prefix>` (UPDATE revoked_at) в src/gram_mq/cli/keys.py + main.py — закрывает эксплуатационный UPDATE MVP (Future: CLI управления ключами)
- [ ] T030 Эндпоинт статистики: GET /api/v1/stats (queue_depth по ботам через контракт брокера, счётчики статусов) в src/gram_mq/api/app.py (Future: эндпоинт статистики)
- [ ] T031 parse_mode и медиа в MessageIn/send_text (Future: parse_mode и медиа)
- [ ] T032 Идемпотентность постановки: клиентский dedup-ключ + окно (Future: идемпотентность)
- [ ] T033 Несколько воркеров: нагрузочная проверка fairness/reclaim, документация запуска N инстансов (Future: несколько воркеров)
- [ ] T034 Rate limiting/квоты API (Future: rate limiting)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: без зависимостей
- **Foundational (Phase 2)**: после Phase 1 — блокирует все stories
- **US1 (Phase 3)**: после Phase 2 (T006 верификатор/генерация)
- **US2 (Phase 4)**: после Phase 2; интеграционная проверка — после US1 (T024 использует выдачу ключа)
- **US3 (Phase 5)**: после Phase 2 (порт T003, адаптер T007, bot_config T011)
- **US4 (Phase 6)**: после US2+US3
- **Polish (Phase 7)**: после US4; **Post-MVP (Phase 8)**: вне цикла → issues

### Within Each User Story

- Тесты раньше реализации и обязаны падать до неё
- Порты/модели → адаптеры/сервисы → точки входа

### Parallel Opportunities

- T003/T004/T011 параллельны; T009/T010 параллельны; T012/T015/T020 — тесты трёх stories параллельны после Phase 2

---

## Implementation Strategy

### MVP First

1. Phase 1–2: Setup + Foundational (T001–T011)
2. Phase 3: US1 ключи (T012–T014)
3. Phase 4: US2 API (T015–T019)
4. **STOP and VALIDATE**: quickstart §2–§3 — ключ выдан, POST ставит в очередь
5. Phase 5: US3 воркер (T020–T023) → quickstart §4–§6
6. Phase 6–7: US4 подсчёт + Polish (T024–T028)
7. Phase 8 → консолидировано в specs/004-post-mvp-hardening (спека-бэклог техдолга)

---

## Notes

- [P] = разные файлы, нет зависимостей от незакрытых задач
- Токены ботов и полные API-ключи не появляются в тестах вывода, логах и БД (FR-008/FR-009, SC-004); в тестах — синтетика вида `10:test-secret` / `gmq_`+сгенерированный
- Конституция II — не режется: ack атомарен, retry/dead_letter только через контракт брокера
- Решения: research.md R1–R12; контракты: contracts/{http-api,telegram-sender,bot-config}.md

---

## Phase 9: Convergence

- [x] T035 (решение оператора 2026-10-11) Поднять DEFAULT_MAX_ATTEMPTS с 5 до 20 в src/gram_mq/settings.py — буфер на всплески в один чат; колонка max_attempts у уже существующих строк журнала (5) носит информационный характер, политику исчерпания применяет воркер из настроек, миграция не требуется
- [x] T036 (решение оператора 2026-10-11) 429 не расходует попытки: BrokerPort.retry получил keyword-only count_attempt=True (дополнение контракта 001, оба адаптера обновлены), process_delivery в src/gram_mq/worker/runner.py для retry_after≠None делает retry строго по retry_after с count_attempt=False — сообщение не может уйти в failed от одних лишь rate-limit-пауз. Тесты: контрактный test_retry_rate_limit_pause_keeps_attempts (memory+postgres), юнит test_rate_limit_never_burns_attempts (max_attempts=2, 4 цикла 429 → attempts=0)
