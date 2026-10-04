---
description: "Task list template for feature implementation"
---

# Tasks: Очередь исходящих сообщений за портом брокера

**Input**: Design documents from `/specs/001-broker-port-postgres/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/broker-port.md, quickstart.md

**Tests**: Включены — спека требует контрактный сюит (FR-010, US6) и конституция требует покрытия поведения порта. Порядок в каждой фиче: тесты раньше реализации, тесты обязаны падать до имплементации.

**Organization**: Задачи сгруппированы по user stories спеки для независимой реализации и проверки.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Можно параллельно (разные файлы, нет зависимостей от незакрытых задач)
- **[Story]**: Принадлежность к user story (US1–US6 по spec.md)
- Точные пути файлов обязательны в описании

## Path Conventions

Single project: `src/`, `tests/` от корня репозитория (см. plan.md → Project Structure).

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Каркас проекта с нуля (в рабочем дереве нет ни pyproject, ни src)

- [ ] T001 Создать pyproject.toml: uv-проект, requires-python ">=3.12", зависимости sqlalchemy>=2, asyncpg, alembic, pydantic-settings; dev-группа pytest, pytest-asyncio, ruff, mypy, coverage; src-layout (tool.setuptools или hatchling, пакет из src/gram_mq)
- [ ] T002 [P] Создать скелет пакета: src/gram_mq/__init__.py, tests/__init__.py, tests/contract/__init__.py
- [ ] T003 [P] Настроить инструменты в pyproject.toml: [tool.pytest.ini_options] asyncio_mode="auto", testpaths=["tests"]; [tool.ruff] line-length=88, select базовый набор (E,F,I,UP,B,ASYNC); [tool.mypy] strict=true; [tool.coverage.report] fail_under=80
- [ ] T004 [P] Дополнить .gitignore: .venv/, __pycache__/, .pytest_cache/, .ruff_cache/
- [ ] T005 Выполнить `uv sync --group dev`, убедиться, что `uv run pytest` и `uv run ruff check .` запускаются на пустом tests/

**Checkpoint**: проект собирается, тестовый прогон зелёный (0 тестов)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Общая инфраструктура: настройки, БД-слой, модели, миграция, контракт порта, каркас контрактных тестов. Блокирует все user stories.

- [ ] T006 Реализовать src/gram_mq/settings.py по стандарту Configuration (RQ-10): BaseSettings с env_prefix="GRAMMQ_"; database_url: SecretStr — required, без дефолта (GRAMMQ_DATABASE_URL), broker_lease_seconds: int = 60 (RQ-1), broker_max_attempts: int = 5 (RQ-2); env-файл — путь из GRAMMQ_ENV_FILE, локальный фолбэк ./.env; реальные env-переменные приоритетнее файла
- [ ] T007 Реализовать src/gram_mq/db.py: async engine (asyncpg) + async_sessionmaker из settings.database_url.get_secret_value() — SecretStr раскрывается ровно в этой одной точке
- [ ] T008 [P] Создать src/gram_mq/models/base.py: DeclarativeBase + общий naming_convention для констрейнтов
- [ ] T009 [P] Создать src/gram_mq/models/bots.py: Bot — bot_slug: string PK; created_at: timestamptz NOT NULL DEFAULT now() (токенов и настроек в таблице нет — конституция, принцип III)
- [ ] T010 [P] Создать src/gram_mq/models/messages.py: Message — id: UUID PK (генерация в приложении, RQ-7); bot_slug: string NOT NULL FK→bots; chat_id: string NOT NULL; payload: JSONB NOT NULL; status: enum NOT NULL (queued/leased/sent/failed); available_at: timestamptz NOT NULL; locked_at, locked_by, lease_expires_at: NULL; attempts: int NOT NULL DEFAULT 0; max_attempts: int NOT NULL DEFAULT 5; telegram_message_id: string NULL; error: text NULL; created_at: timestamptz NOT NULL; sent_at: timestamptz NULL
- [ ] T011 [P] Создать src/gram_mq/models/claim_state.py: BotClaimState — bot_slug: string PK FK→bots; last_claim_at: timestamptz NULL (RQ-4)
- [ ] T012 Настроить alembic/: async env.py (по database_url), создать начальную миграцию: таблицы bots, messages, bot_claim_state + три частичных индекса (RQ-6): messages(status, available_at) WHERE status='queued'; messages(lease_expires_at) WHERE status='leased'; messages(bot_slug) WHERE status IN ('queued','leased')
- [ ] T013 Реализовать src/gram_mq/ports/broker.py строго по contracts/broker-port.md: OutboundMessage (id: UUID, bot_slug, chat_id, payload: dict), Delivery (id, bot_slug, chat_id, payload, context: dict — непрозрачен для домена), Protocol BrokerPort с сигнатурами enqueue/claim(worker_id)/ack/retry(delay, reason)/dead_letter(reason)/queue_depth; docstring-инварианты 1–6 из контракта
- [ ] T014 Создать tests/conftest.py: фикстура `clock` (инжектируемые часы для InMemoryBroker); фикстура `register_bots` (создать строки bots + bot_claim_state через адаптер/сессию); параметризованная фабрика `make_broker` — InMemoryBroker(clock) всегда, PostgresBroker при TEST_DATABASE_URL (иначе skip)
- [ ] T015 Создать tests/contract/test_broker_port.py: каркас с параметризацией по адаптерам (ids=["memory","postgres"]) и пустым smoke-тестом на существование фикстуры

**Checkpoint**: фундамент готов — `uv run pytest tests/contract -q` зелёный; user stories стартуют

---

## Phase 3: User Story 1 - Постановка сообщения в очередь (Priority: P1) 🎯 MVP

**Goal**: enqueue работает в обоих адаптерах: строка queued в журнале, постановка переживает пересоздание брокера, неизвестный бот отвергается; queue_depth считает по определению FR-009

**Independent Test**: контракный тест US1 зелёный против in-memory без какой-либо инфраструктуры (quickstart.md, п.1)

### Tests for User Story 1 (сначала, обязаны падать)

- [ ] T016 [P] [US1] Тест в tests/contract/test_broker_port.py::TestEnqueue: (а) enqueue → строка status=queued, available_at<=now, attempts=0; (б) пересоздание брокера (memory: новый экземпляр над тем же состоянием / pg: новая сессия) — сообщение на месте и доступно (журнал=очередь, сценарий 1.2); (в) enqueue для незарегистрированного bot_slug — исключение (FK); (г) queue_depth==1 после постановки и ==0 для пустого бота (сценарий 1.1); (д) производительность: enqueue при глубине очереди 10 000 укладывается в бюджет 50 мс — время не растёт с глубиной (SC-004)

### Implementation for User Story 1

- [ ] T017 [US1] Реализовать src/gram_mq/adapters/memory/broker.py: InMemoryBroker — хранилище строк в памяти, реестр ботов, инжектируемый clock; методы enqueue (status=queued, available_at=clock.now(), attempts=0, max_attempts из настроек) и queue_depth (COUNT queued+leased по bot_slug, FR-009)
- [ ] T018 [US1] Реализовать src/gram_mq/adapters/postgres/broker.py: PostgresBroker.enqueue (INSERT со значениями как в memory-адаптере) и queue_depth (SELECT COUNT(*) с частичным индексом bot_slug WHERE status IN ('queued','leased'))
- [ ] T019 [US1] Прогнать `uv run pytest tests/contract -q` (in-memory зелёный; postgres skip без env), `uv run ruff check --fix . && uv run ruff format .`

**Checkpoint**: MVP — постановка и глубина работают независимо (quickstart.md п.3 smoke)

---

## Phase 4: User Story 2 - Захват с лизом и справедливостью (Priority: P1)

**Goal**: claim отдаёт доступные строки в исключительное владение (SKIP LOCKED), пустая очередь → None, распределение между ботами без голодания (SC-003)

**Independent Test**: контракный тест US2: серия захватов при 1000+ строк у бота A и по одной у B и C — в первых 10 захватах ≥2 разных бота

### Tests for User Story 2

- [ ] T020 [P] [US2] Тесты в tests/contract/test_broker_port.py::TestClaim: (а) claim → status=leased, locked_by=worker_id, lease_expires_at=now+лиз(RQ-1); (б) повторный claim чужим worker_id при живом лизе не отдаёт строку; (в) пустая очередь → None; (г) справедливость: бот A — 1000 строк, B и C — по одной → первые 10 захватов содержат ≥2 ботов (SC-003)

### Implementation for User Story 2

- [ ] T021 [US2] PostgresBroker.claim в src/gram_mq/adapters/postgres/broker.py: шаг 1 — выбор бота (ORDER BY last_claim_at NULLS FIRST FROM bot_claim_state semi-join доступные messages, RQ-4); шаг 2 — UPDATE messages SET leased-поля WHERE id IN (SELECT ... WHERE bot_slug=:b AND status='queued' AND available_at<=now() ORDER BY available_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING (RQ-5, без ветки истёкших — она в US3); обновить bot_claim_state.last_claim_at в той же транзакции
- [ ] T022 [US2] InMemoryBroker.claim в src/gram_mq/adapters/memory/broker.py: блокировка экземпляра, fairness-ротация бот→строка, leased-поля через clock
- [ ] T023 [US2] Прогнать сюит, ruff

**Checkpoint**: захват эксклюзивен и справедлив

---

## Phase 5: User Story 3 - Переживание падения воркера (Priority: P1)

**Goal**: истёкший лиз автоматически возвращает строку в работу любому воркеру (FR-005, at-least-once)

**Independent Test**: тест «падение воркера»: leased + истёкший lease_expires_at → claim другого воркера возвращает ту же строку (SC-001)

### Tests for User Story 3

- [ ] T024 [P] [US3] Тесты в tests/contract/test_broker_port.py::TestLeaseExpiry: (а) leased, clock.advance(лиз+ε) (pg: broker_lease_seconds=0.2 и sleep 0.3, RQ-8) → claim worker_id="w2" возвращает ту же строку с новым лизом; (б) живой лиз → строка не отдаётся (сценарий 3.2)

### Implementation for User Story 3

- [ ] T025 [US3] Расширить условие выбора в PostgresBroker.claim: OR (status='leased' AND lease_expires_at < now()) — прямой реклейм в новый leased без промежуточного статуса (RQ-5); то же в InMemoryBroker.claim
- [ ] T026 [US3] Прогнать сюит, ruff

**Checkpoint**: принцип II конституции подтверждён тестом (переживание падения)

---

## Phase 6: User Story 4 - Фиксация результата: ack / retry / dead_letter (Priority: P1)

**Goal**: атомарный ack, retry с принудительным backoff, терминальный failed с причиной

**Independent Test**: три ветки теста US4 зелёные: ack, retry-до/после available_at, dead_letter после max_attempts

### Tests for User Story 4

- [ ] T027 [P] [US4] Тесты в tests/contract/test_broker_port.py::TestOutcome: (а) ack (context["telegram_message_id"]="777") → status=sent, telegram_message_id=777, sent_at NOT NULL одной операцией; повторный ack отправленной строки → ValueError (RQ-9); (б) retry(delay=30, reason) → queued, available_at≈now+30, attempts+1, лиз сброшен; до истечения claim не отдаёт, после (clock.advance) отдаёт; (в) dead_letter(reason) → failed с error=reason; claim не возвращает; строка в журнале (сценарии 4.1–4.3)

### Implementation for User Story 4

- [ ] T028 [US4] PostgresBroker: ack — условный UPDATE ... WHERE id=:id AND status='leased' (RQ-9); retry — UPDATE в queued с available_at=now()+delay, attempts+1, сброс лиза; dead_letter — UPDATE в failed, error=reason, сброс лиза
- [ ] T029 [US4] InMemoryBroker: эквиваленты трёх операций с той же семантикой
- [ ] T030 [US4] Прогнать сюит, ruff

**Checkpoint**: полный жизненный цикл сообщения работает

---

## Phase 7: User Story 5 - Глубина очереди для оператора (Priority: P2)

**Goal**: семантика queue_depth по определению FR-009 зафиксирована тестами на всех переходах

**Independent Test**: глубина: 0 у пустого; queued и leased считаются; после ack уменьшается; sent/failed не считаются

### Tests for User Story 5

- [ ] T031 [P] [US5] Тесты в tests/contract/test_broker_port.py::TestQueueDepth: (а) пустой бот → 0; (б) после enqueue → 1; после claim (leased) → всё ещё 1 (захваченное — недоставленное); (в) после ack → 0; (г) после dead_letter → 0 (failed не считается); (д) второй бот изолирован (сценарии 5.1–5.2, изоляция ботов)

### Implementation for User Story 5

- [ ] T032 [US5] Если тесты вскрывают расхождение — привести реализации queue_depth обоих адаптеров к FR-009 (COUNT статусов queued+leased)
- [ ] T033 [US5] Прогнать сюит, ruff

**Checkpoint**: наблюдаемость подтверждена

---

## Phase 8: User Story 6 - Эквивалентность адаптеров (Priority: P2)

**Goal**: один сюит, одинаковый результат против обоих адаптеров (FR-010, SC-002); postgres-прогон за skipif-шлюзом

**Independent Test**: `uv run pytest tests/contract -q` зелёный (memory); с TEST_DATABASE_URL тот же сюит зелёный (postgres)

### Tests / Verification for User Story 6

- [ ] T034 [US6] Убедиться, что все сценарии US1–US5 живут в одном параметризованном сюите (без дублей в отдельных файлах); при необходимости перенести в tests/contract/test_broker_port.py
- [ ] T035 [US6] Проверить skip-поведение: без TEST_DATABASE_URL postgres-параметризация репортит skip, memory — проходит; прогон с TEST_DATABASE_URL (при наличии БД) по quickstart.md п.2
- [ ] T036 [US6] Выполнить smoke из quickstart.md п.3 (постановка → глубина → захват → эксклюзивность → ack) — вывод smoke OK

**Checkpoint**: обе реализации эквивалентны по одному сюиту

---

## Phase 9: Polish & Cross-Cutting Concerns

- [ ] T037 [P] Финальные гейты: `uv run ruff check . && uv run ruff format --check .` — чисто; `uv run mypy .` — strict, 0 ошибок; `uv run coverage run -m pytest && uv run coverage report --fail-under=80` — порог пройден, каждый публичный метод порта покрыт хотя бы одним тестом
- [ ] T038 Полная валидация по quickstart.md: пункты 1 (memory-сюит), 2 (postgres при env), 4 (падение воркера), 5 (справедливость) — все зелёные
- [ ] T039 Обновить README.md: раздел о модуле очереди (как запустить контрактные тесты, как поднять тестовую БД) — без деталей реализации
- [ ] T040 Ревью кода всей фичи целиком: полный diff реализации всех user stories; коммит фичи — один раз, после одобрения ревьюером

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: без зависимостей, старт немедленно
- **Foundational (Phase 2)**: после Phase 1 — БЛОКИРУЕТ все user stories
- **User Stories (Phases 3–8)**: после Phase 2; порядок по приоритету P1→P2; внутри P1 порядок US1→US2→US3→US4 содержательный (claim строится на enqueue; expiry — на claim; outcome — на claim)
- **Polish (Phase 9)**: после всех stories

### User Story Dependencies

- **US1 (P1)**: после Foundational — независима, MVP
- **US2 (P1)**: после Foundational; использует enqueue из US1 (тестовые данные)
- **US3 (P1)**: использует claim из US2 (расширяет условие выбора)
- **US4 (P1)**: использует claim из US2
- **US5 (P2)**: поверх US1–US4 (переходы состояний)
- **US6 (P2)**: консолидация и эквивалентность — после US1–US5

### Within Each User Story

- Тесты раньше реализации и обязаны падать до неё
- Модели/типы → адаптеры → прогон сюита
- Story закрыта — чекпоинт-прогон зелёный; без коммита: ревью и коммит — один раз по завершении всей фичи (T040)

### Parallel Opportunities

- Phase 1: T002, T003, T004 параллельны после T001
- Phase 2: T008–T011 (модели) параллельны после T006–T007
- Внутри каждой story тест-задача [P] пишется параллельно с подготовкой; реализации memory/postgres адаптеров — параллелизуемы по файлам после теста
- Разные stories — последовательно (общие файлы адаптеров)

---

## Parallel Example: User Story 2

```bash
# После T020 (тесты написаны и падают):
Task T021: PostgresBroker.claim в src/gram_mq/adapters/postgres/broker.py
Task T022: InMemoryBroker.claim в src/gram_mq/adapters/memory/broker.py
# Разные файлы — можно параллельно; T023 (прогон) после обоих
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1: Setup (T001–T005)
2. Phase 2: Foundational (T006–T015)
3. Phase 3: US1 (T016–T019)
4. **STOP and VALIDATE**: quickstart.md п.3 smoke — постановка и глубина работают
5. Далее инкрементально: US2 → US3 → US4 → US5 → US6 → Polish

### Incremental Delivery

Каждая следующая story добавляет операции контракта, не ломая предыдущие (сюит накапливается, регресс ловится каждым прогоном).

---

## Notes

- [P] = разные файлы, нет зависимостей от незакрытых задач
- [Story] метки связывают задачи со stories спеки (трассируемость FR/SC ↔ задачи)
- Ревью всей фичи целиком, а не отдельных задач: коммит — один раз после ревью и одобрения всей реализации (T040)
- Референс при затруднениях: ветка main_copy (ports/broker.py, adapters/postgres/broker.py, adapters/memory/broker.py) — сверять семантику, не копировать вслепую (референс не содержит fairness-таблицы и реклейма в claim)
