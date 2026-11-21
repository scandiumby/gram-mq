---
description: "Task list: Двухэтапный бутстрап API-ключа"
---

# Tasks: Двухэтапный бутстрап API-ключа

**Input**: Design documents from `/specs/005-api-key-bootstrap/`

**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/{cli-key-prepare,key-bootstrap-endpoint}.md, quickstart.md

**Tests**: Включены — конституция (Testing) требует покрытия публичных функций и ≥80%; спека проверяет SC-001–SC-004 тестами и инспекцией. Порядок: тесты раньше реализации (RED), реализация закрывает (GREEN).

**Organization**: Задачи сгруппированы по user stories спеки (US1 локальная генерация → US2 эндпоинт активации → US3 повторные/посторонние вызовы). Общий знаменатель US1/US2 — примитивы материала — вынесен в Foundational. US2 по коду зависит только от Foundational, но по проверке использует материал US1.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Можно параллельно (разные файлы, нет зависимостей от незакрытых задач)
- **[Story]**: Принадлежность к user story спеки
- Точные пути файлов обязательны в описании

## Path Conventions

Single project: `src/`, `tests/` от корня репозитория (см. plan.md → Project Structure).

## Phase 1: Setup

**Purpose**: Не требуется: новых зависимостей, пакетов и скелетов нет
(plan.md — «новых зависимостей нет», все целевые файлы существуют);
миграций нет (используется существующая `gmq_api_keys`).

---

## Phase 2: Foundational (примитивы значения активации)

**Purpose**: Формат `prefix:хэш` — общий знаменатель US1 (строит) и US2
(парсит). Блокирует US1 и US2.

- [x] T001 RED: tests/unit/test_key_bootstrap.py — новый файл; тесты
  `format_key_material`/`parse_key_material` из
  specs/005-api-key-bootstrap/contracts/key-bootstrap-endpoint.md:
  round-trip (format → parse возвращает исходные prefix и хэш); отказ
  (parse → None): нет `:`; более одного `:`; пустой prefix или пустой
  хэш; prefix не матчит `^gmq_[A-Za-z0-9_-]{8}$` (нет `gmq_`, не 12
  символов, посторонние символы); хэш без префикса `$argon2id$`; длина
  хэша > 128. Убедиться, что тесты падают (функций ещё нет)
- [x] T002 GREEN: src/gram_mq/api/auth.py — рядом с существующими
  примитивами ключа: константа `PREFIX_RE = ^gmq_[A-Za-z0-9_-]{8}$`,
  `format_key_material(prefix: str, key_hash: str) -> str` (строка
  `"<prefix>:<key_hash>"`), `parse_key_material(value: str) ->
  tuple[str, str] | None` — правила дословно из data-model.md: значение
  содержит ровно один `:` (иначе None); prefix — `PREFIX_RE`; key_hash —
  начинается с `$argon2id$` и длина ≤ 128 (ширина колонки `key_hash`);
  любое нарушение → None без исключений

**Checkpoint**: `uv run pytest tests/unit/test_key_bootstrap.py -q` зелёный; US1 и US2 можно начинать

---

## Phase 3: User Story 1 - Локальная генерация ключа без сервера (Priority: P1) 🎯 MVP

**Goal**: `gram-mq key prepare` печатает plaintext один раз + prefix + одно
готовое к копированию значение активации; без БД и настроек (FR-001–FR-004).

**Independent Test**: quickstart.md §1 — `unset GRAMMQ_DATABASE_URL; uv run
gram-mq key prepare` → код 0, ключ/prefix/материал напечатаны; US1
сценарии 1–4.

### Tests for User Story 1

- [x] T003 [US1] RED: tests/unit/test_keys_cli.py — дополнить: (а)
  `run_key_prepare()` — синхронная (не coroutine), возвращает
  `(key, prefix, material)`; `material == format_key_material(prefix,
  hash)` и `parse_key_material(material)` round-trip'ится; хэш
  верифицируется с plaintext (`PasswordHasher.verify`, как
  test_keys_cli.py:CHEAP_HASHER); два вызова дают разные ключи и
  материалы; (б) CLI `main(["key", "prepare"])` под `monkeypatch.delenv`
  всех `GRAMMQ_*` (в т.ч. без `GRAMMQ_DATABASE_URL`) → exit 0, в stdout
  ключ ровно один раз, строка `prefix:` и строка материала (contracts/
  cli-key-prepare.md). Убедиться, что падает

### Implementation for User Story 1

- [x] T004 [US1] GREEN: src/gram_mq/cli/keys.py —
  `run_key_prepare() -> tuple[str, str, str]`: `generate_api_key()` →
  `hash_key()` → `format_key_material()`; чистая синхронная функция без
  параметров и без БД; src/gram_mq/cli/main.py — подкоманда `key
  prepare` в парсере и диспетче (раньше любых обращений к
  `get_settings`, по образцу `_key_add`), вывод по contracts/
  cli-key-prepare.md (plaintext один раз, `prefix: ...`, строка
  «Значение активации (для GRAMMQ_KEY_BOOTSTRAP_MATERIAL):» + материал),
  exit 0; `Settings` не инстанцировать (FR-002)

**Checkpoint**: `uv run pytest tests/unit -q` зелёный; MVP готов — материал генерируется локально без сервера

---

## Phase 4: User Story 2 - Активация ключа через выключаемый эндпоинт (Priority: P2)

**Goal**: `POST /bootstrap/api-key` — 201/409/404/503 по контракту;
материал только из настройки; активированный ключ сразу проходит
действующий `X-API-Key` (FR-005–FR-013).

**Independent Test**: quickstart.md §2–§4; US2 сценарии 1–5 (юнит —
FakeActivator, интеграция — T011).

### Tests for User Story 2

- [x] T005 [P] [US2] RED: tests/unit/test_key_bootstrap.py — дополнить
  (httpx AsyncClient + ASGITransport, `create_app(..., key_bootstrap=
  KeyBootstrap(enabled, material), key_activator=FakeActivator)`):
  404 — bootstrap-параметры не переданы и `enabled=False` (маршрут
  «не существует»); 201 — валидный материал: в FakeActivator попало
  `(prefix, хэш)`, тело `{"status":"activated","prefix":...}`, хэша и
  материала в теле нет; 409 — `activate` вернул False; 503 — material
  None и битый материал (`gmq_bad:xxx`), в обоих случаях FakeActivator
  не вызывался; сквозная композиция US2 сценария 3 — dict-активатор
  кормит `ApiKeyVerifier.fetch` → `verify(plaintext из material)` True.
  Убедиться, что падает

### Implementation for User Story 2

- [x] T006 [P] [US2] GREEN: src/gram_mq/settings.py — в `Settings`:
  `key_bootstrap_enabled: bool = False` (не секрет; дефолт «выключено»
  версируется в коде — конституция Configuration) и
  `key_bootstrap_material: SecretStr | None = None` (секретное, без
  дефолтного значения; `GRAMMQ_KEY_BOOTSTRAP_ENABLED` /
  `GRAMMQ_KEY_BOOTSTRAP_MATERIAL`)
- [x] T007 [US2] GREEN: src/gram_mq/api/app.py — `class
  KeyActivator(Protocol)` с `async def activate(self, prefix: str,
  key_hash: str) -> bool` (True — вставлено, False — префикс уже есть);
  frozen `KeyBootstrap(enabled: bool, material: SecretStr | None)`;
  параметры `create_app(..., key_bootstrap: KeyBootstrap | None = None,
  key_activator: KeyActivator | None = None)`; маршрут
  `POST /bootstrap/api-key` по contracts/key-bootstrap-endpoint.md:
  выключен/не задан → 404 `{"detail":"not found"}`; материал None или
  `parse_key_material → None` → 503 `{"detail":"bootstrap material
  missing or invalid"}` (одно сообщение, без эха материала);
  `activate → False` → 409; успех → 201 `{"status":"activated",
  "prefix":...}`; логи research R8 (info/warning/error, только prefix,
  без материала); без Depends на Settings
- [x] T008 [US2] GREEN: src/gram_mq/api/main.py — в
  `build_default_app()`: сборка `KeyBootstrap(enabled=
  settings.key_bootstrap_enabled, material=
  settings.key_bootstrap_material)`; DB-адаптер `activate(prefix,
  key_hash) -> bool` по образцу `fetch`: INSERT `ApiKey` через
  `db.get_session_factory()` в транзакции, `IntegrityError → False`
  (конкурентные вызовы закрывает PK prefix)
- [x] T009 [US2] GREEN: `uv run pytest tests/unit -q` — весь юнит-суит
  зелёный, существующие тесты API не сломаны (новые параметры
  `create_app` опциональны)

**Checkpoint**: двухэтапный сценарий работает целиком (quickstart §1–§4)

---

## Phase 5: User Story 3 - Повторные и посторонние вызовы безопасны (Priority: P3)

**Goal**: Повтор — 409 без мутаций; конкурентные — ровно одна строка; в
ответах никогда нет хэша/материала (FR-010, FR-014, US3).

**Independent Test**: US3 сценарии 1–3; quickstart §3 (повторный POST) и §6.

### Tests for User Story 3

- [x] T010 [US3] RED+GREEN: tests/unit/test_key_bootstrap.py — дополнить:
  повторный POST с тем же материалом → 409, содержимое FakeActivator не
  изменилось; конкурентные — `asyncio.gather` двух POST при активаторе с
  PK-семантикой (`dict.setdefault`-подобной: второй вставляющий видит
  занятый prefix и возвращает False) → ровно один 201 и один 409; во
  всех ответах (201/404/409/503) нет ни хэша, ни материала (US3
  сценарий 3). Тесты обязаны проходить на реализации US2 по контракту —
  падение сигнализирует об отклонении от FR-010
- [x] T011 [US3] DB-gated: tests/integration/test_key_bootstrap_db.py —
  новый файл по образцу tests/integration/test_api_db.py (skip без
  GRAMMQ_TEST_DATABASE_URL/TEST_DATABASE_URL, conftest.TEST_DATABASE_URL);
  production-адаптер `activate` из src/gram_mq/api/main.py на реальной
  БД: вставка реального материала (дешёвый hasher для скорости) → True и
  строка в `gmq_api_keys`; повторная вставка того же prefix → False
  (живой IntegrityError/PK); сквозная: `ApiKeyVerifier` над той же БД
  верифицирует plaintext (US2 сценарий 3 в реальном окружении);
  очистка строки в фикстуре
- [x] T012 [US3] GREEN: `uv run pytest -q` — весь сюит (с DB-частью при
  настроенной .env.test)

**Checkpoint**: все stories независимо функциональны; safety-свойства зафиксированы тестами

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Документация и финальные гейты поверх всех stories.

- [x] T013 [P] .env.example — задокументировать `GRAMMQ_KEY_BOOTSTRAP_ENABLED`
  (дефолт false, выключает маршрут) и `GRAMMQ_KEY_BOOTSTRAP_MATERIAL`
  (секрет, `prefix:хэш` из `gram-mq key prepare`, без дефолта; окно
  операции: включил → активировал → выключил) в стиле существующих
  комментариев файла
- [x] T014 Прогнать specs/005-api-key-bootstrap/quickstart.md §0–§6 на
  локальной БД; зафиксировать расхождения (если появились) в quickstart
  или задачах
- [x] T015 Финальный гейт: `uv run check` (ruff + format + mypy --strict)
  зелёный; покрытие ≥80% не просело (`uv run coverage ...` по README);
  сверка FR-001–FR-016 и SC-001–SC-004 по specs/005-api-key-bootstrap/
  spec.md; `gram-mq key add` не изменился (FR-016)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Foundational (Phase 2)**: старт немедленно (Setup пуст); блокирует US1 и US2
- **US1 (Phase 3)**: после Phase 2
- **US2 (Phase 4)**: после Phase 2 по коду; для сквозной проверки руками использует материал US1 (юнит-тесты US2 самодостаточны — материал строится `format_key_material`)
- **US3 (Phase 5)**: после US2 (проверяет её контракт); T011 — после T008
- **Polish (Phase 6)**: после всех stories

### Within Each User Story

- Тесты (RED) раньше реализации; реализация закрывает (GREEN)
- Примитивы → CLI/маршрут → сборка адаптера
- Стори завершена, пока checkpoint не зелёный

### Parallel Opportunities

- T005 (тест-файл) ‖ T006 (settings.py) — разные файлы
- T013 (документация) ‖ любые задачи кода — разные файлы
- US1 (cli/) и US2 (api/, settings.py) — разные файлы, можно параллельно после Phase 2

---

## Parallel Example: User Story 2

```bash
# После Phase 2 — параллельно (разные файлы):
Task: "T005 [P] [US2] RED: tests/unit/test_key_bootstrap.py — матрица эндпоинта"
Task: "T006 [P] [US2] GREEN: src/gram_mq/settings.py — два новых поля"
# Затем последовательно (зависят от контрактов друг друга):
Task: "T007 [US2] GREEN: src/gram_mq/api/app.py — порт, снапшот, маршрут"
Task: "T008 [US2] GREEN: src/gram_mq/api/main.py — сборка адаптера"
```

---

## Implementation Strategy

### MVP First (Phase 2 + User Story 1)

1. Phase 2: примитивы материала (T001–T002)
2. Phase 3: `gram-mq key prepare` (T003–T004)
3. **STOP and VALIDATE**: quickstart §1 без БД — ключ и материал
   генерируются локально (самостоятельная ценность: материал можно
   вставить в БД и руками)

### Incremental Delivery

1. Foundational → US1 → проверить независимо
2. + US2 → двухэтапный сценарий целиком (quickstart §1–§4)
3. + US3 → safety-свойства и DB-интеграция
4. Polish → документация, quickstart-прогон, гейты

---

## Notes

- [P] задачи = разные файлы, нет зависимостей от незакрытых задач
- Тесты пишутся раньше и обязаны падать до реализации (кроме T010 —
  верификация контракта уже реализованного US2 и T011 — DB-gated)
- Секреты в тестах не используются; DB-тесты — только через
  GRAMMQ_TEST_DATABASE_URL/.env.test (конституция Testing)
- Проверять фичу целиком (не отдельные задачи) перед коммитом; коммит —
  один после приёмки ревьюером
- Существующий `key add` и его тесты не меняются (FR-016)

---

## Phase 7: Convergence

**Purpose**: Сверка кодовой базы со spec/plan/tasks после имплементации
($speckit-converge, 2026-10-18). Единственная находка — LOW, letter-level.

- [x] T016 Добавить датированную приписку (прецедент: «дополнение 2026-10-11»
  в contracts/telegram-sender.md спеки 003) в specs/005-api-key-bootstrap/
  plan.md (Project Structure) и research.md (R5): порт KeyActivator
  реализован как коллэйбл-алиас `Callable[[str, str], Awaitable[bool]]`,
  а не Protocol-класс с методом `.activate` — форма изменена при живом
  прогоне quickstart (T014) после дефекта сборки; семантика порта
  (инъекция, True/False, сборка адаптера в api/main.py) без изменений
  per plan:R5 (contradicts)
