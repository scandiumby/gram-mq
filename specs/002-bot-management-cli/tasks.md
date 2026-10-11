---
description: "Task list: Управление ботами через CLI (MVP: gram-mq bot add)"
---

# Tasks: Управление ботами через CLI (MVP: gram-mq bot add)

**Input**: Design documents from `/specs/002-bot-management-cli/`

**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/cli-bot-add.md, quickstart.md

**Tests**: Включены — конституция (Testing) требует покрытия публичных функций и быстрых тестов на генерацию slug и валидацию токена; спека прямо требует SC-005. Порядок: тесты раньше реализации, тесты обязаны падать до имплементации.

**Organization**: Задачи сгруппированы по фазам; единственная story MVP — US1 (`bot add`). US2 и FR-013 — пост-MVP фаза (тех долг → issues), реализация их запрещена до явного решения.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Можно параллельно (разные файлы, нет зависимостей от незакрытых задач)
- **[Story]**: Принадлежность к user story спеки
- Точные пути файлов обязательны в описании

## Path Conventions

Single project: `src/`, `tests/` от корня репозитория (см. plan.md → Project Structure).

## Phase 1: Setup

**Purpose**: Точка входа CLI и скелеты пакетов.

- [x] T001 В pyproject.toml добавить консольный скрипт `gram-mq = "gram_mq.cli.main:main"` в [project.scripts]; дополнить .gitignore строкой `bots/` (конституция, принцип III)
- [x] T002 [P] Создать скелеты: src/gram_mq/cli/__init__.py, tests/unit/__init__.py, tests/integration/__init__.py

**Checkpoint**: `uv sync --group dev` проходит, `uv run check` зелёный на пустом cli-пакете

---

## Phase 2: Foundational (чистые функции US1: slug и токен)

**Purpose**: Форматные контракты data-model.md без ввода-вывода визарда. Блокирует US1.

- [x] T003 [P] RED: tests/unit/test_slug.py — контракт bot_slug (data-model.md): regex `^[0-9]{4}(-[a-z-]+){2}$`, длина ≤ 30; `42` → префикс `0042` (zero-pad); слова пары различны; ГСЧ инжектируется (детерминизм); SC-005 — 100 генераций на одном 4-значном префиксе попарно различны; коллизии против колбэка `is_taken` разрешаются перегенерацией; исчерпание 50 попыток — исключение
- [x] T004 [P] RED: tests/unit/test_token.py — валидация формата `{id}:{secret}` (data-model.md): без `:`, два `:`, пустой id/secret, нечисловой id, внешний trim, встроенный `\n` — ошибка; запись env-файла (GRAMMQ_BOT_TOKEN/GRAMMQ_BOT_NAME, перевод строки в конце) и чтение — round-trip; `=` в имени не ломает парсинг (деление по первому `=`); создание файла O_EXCL: существующий путь — отказ без перезаписи; права созданного файла 0600; чтение всех `bots/.env-*` для сверки дублей возвращает id токенов и пропускает нечитаемые/невалидные файлы
- [x] T005 GREEN: src/gram_mq/cli/dune_words.py — встроенный вордлист: не менее 120 слов Dune-тематики, каждое `^[a-z][a-z-]{1,11}$`, без дублей (комментарий-обоснование выбора слов не нужен — список и есть контракт)
- [x] T006 GREEN: src/gram_mq/cli/slug.py — `generate_bot_slug(token_id: str, *, is_taken: Callable[[str], bool] | None, rng: random.SystemRandom | None) -> str` по контракту T003 (последние 4 цифры zero-pad + 2 различных слова, ≤ 50 попыток, иначе `SlugExhaustedError`)
- [x] T007 GREEN: src/gram_mq/cli/token.py — `validate_token_format(s: str) -> tuple[str, str] | None`, `write_token_file(path, token, name)` (os.open O_EXCL 0o600, os.fchmod, flush+fsync), `read_token_file(path) -> tuple[str, str] | None` (None = повреждён), `collect_registered_token_ids(bots_dir) -> set[str]` (glob `.env-*`, невалидные пропускаются)

**Checkpoint**: `uv run pytest tests/unit -q` зелёный; секрет не встречается в выводе тестов

---

## Phase 3: User Story 1 - Регистрация бота визардом (Priority: P1) 🎯 MVP

**Goal**: `gram-mq bot add` регистрирует бота атомарно: файл 0600 + строка реестра, либо ничего (FR-009).

**Independent Test**: quickstart.md §1–§4 — регистрация, дубль, отмена, отсутствие секрета в выводе.

### Tests for User Story 1

- [x] T008 [P] [US1] RED: tests/unit/test_wizard.py — визард с инжектированными input/getpass, фейковым реестром (Callable) и tmp-каталогом bots/: happy path создаёт файл 0600 + вызывает вставку slug; невалидный токен → повторный запрос, затем успех; пустое имя → повторный запрос; дубль id токена (существующий файл) → DuplicateTokenError, артефактов нет; сбой вставки реестра → файл удалён, система в исходном состоянии; KeyboardInterrupt на шаге токена → отмена, артефактов нет; итоговый вывод содержит имя, slug, путь и не содержит секретную часть (SC-002, FR-010)

### Implementation for User Story 1

- [x] T009 [US1] src/gram_mq/settings.py: поле `bots_dir: str = "./bots"` (GRAMMQ_BOTS_DIR, не секрет, versioned default — конституция Configuration)
- [x] T010 [US1] src/gram_mq/cli/wizard.py — `run_bot_add(settings, *, session_factory, input_fn, token_input_fn, rng) -> BotAddResult`: предусловия каталога (владелец == euid, это каталог, mkdir 0700), шаги визарда по contracts/cli-bot-add.md, сверка дублей (FR-012), генерация slug с проверкой реестра+ФС (FR-007), файл → INSERT Bot → компенсация unlink при сбое/отмене (FR-009), вывод итога без токена (FR-010); KeyboardInterrupt → cleanup, код 130
- [x] T011 [US1] src/gram_mq/cli/main.py — argparse `bot add`; `bot list` → сообщение «появится после US2 (post-MVP)»; exit-коды контракта: 0/2/3/4/130 (contracts/cli-bot-add.md); `if __name__ == "__main__"` не нужен (консольный скрипт), но добавить `python -m gram_mq` поддержку при нулевой стоимости
- [x] T012 [US1] GREEN: `uv run pytest tests/unit -q` зелёный
- [x] T013 [P] [US1] tests/integration/test_registry_db.py — реальный INSERT через db.get_session_factory в тестовую БД (skip без GRAMMQ_TEST_DATABASE_URL/TEST_DATABASE_URL, конституция Testing): строка появляется в gmq_bots с bot_slug; повторная вставка того же slug → IntegrityError поглощается циклом перегенерации визарда

**Checkpoint**: 🎯 **MVP STOP-POINT** — US1 работает изолированно; далее только Polish, пост-MVP фаза не реализуется

---

## Phase 4: Polish & Cross-Cutting

- [x] T014 Гейты: `uv run check` (ruff check + format, mypy --strict) и `uv run coverage run -m pytest && uv run coverage report` ≥ 80% — зелёные; при падении — исправить код, не гейт
- [x] T015 Прогнать quickstart.md §1–§4 на тестовой БД; зафиксировать результат в файле specs/002-bot-management-cli/checklists/requirements.md (Notes)

---

## Phase 5: Post-MVP (Tech Debt — НЕ РЕАЛИЗОВЫВАТЬ в этом цикле)

> Задачи ниже сознательно отложены (spec.md → Future Enhancements, Session 2026-10-11). `$speckit-implement` их не выполняет; консолидированы в specs/004-post-mvp-hardening/spec.md (трассировка: T016 → FR-001, T017 → FR-002).

- [ ] T016 [US2] Реализовать `gram-mq bot list` в src/gram_mq/cli/main.py + src/gram_mq/cli/listing.py: список ботов (slug, имя из файла, дата регистрации, готовность = файл существует И токен валиден — тот же контракт формата FR-005), пустой реестр — пустой список не-ошибка (Future US2, восстановленный FR-011/SC-003)
- [ ] T017 Исключительная блокировка каталога bots/ на время bot add (flock, отказ параллельному мутатору до начала ввода, снятие при завершении/отмене/аварии; читатели не блокируются) — закрывает гонку дублей из Edge Cases (Future FR-013)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: без зависимостей
- **Foundational (Phase 2)**: после Phase 1 — блокирует US1 (контракты формата)
- **US1 (Phase 3)**: после Phase 2
- **Polish (Phase 4)**: после US1
- **Post-MVP (Phase 5)**: вне этого цикла — в GitHub issues

### Within Each User Story

- Тесты раньше реализации и обязаны падать до неё (T003/T004 → T005–T007; T008 → T009–T012)
- Чистые контракты (slug/token) до визарда; визард до точки входа

### Parallel Opportunities

- T003 и T004 параллельны (разные файлы тестов)
- T013 параллелен T014-подготовке (разные файлы)

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1: Setup (T001–T002)
2. Phase 2: Foundational (T003–T007)
3. Phase 3: US1 (T008–T013)
4. **STOP and VALIDATE**: quickstart.md §1–§4 — регистрация/дубль/отмена/секрет
5. Phase 4: Polish (T014–T015)
6. Phase 5 → консолидировано в specs/004-post-mvp-hardening (спека-бэклог техдолга)

### Incremental Delivery

US1 — единственная story MVP; следующие инкременты (US2, блокировка) приходят из issues отдельными циклами.

---

## Notes

- [P] = разные файлы, нет зависимостей от незакрытых задач
- [Story] метки связывают задачи со stories спеки (трассируемость FR/SC ↔ задачи)
- Ревью всей фичи целиком: коммит — один раз после ревью и одобрения реализации
- Токен (в т.ч. тестовый секрет) не должен появляться в выводе тестов и логах (FR-008); в тестах — синтетические значения вида `10:test-secret`
- Ссылка-обоснование решений: research.md R1–R12; контракт интерфейса: contracts/cli-bot-add.md

---

## Phase 6: Convergence

- [x] T018 Исправление интеграции с 001 (найдено в эксплуатации 2026-10-11): SqlAlchemyBotRegistry.add в src/gram_mq/cli/wizard.py вставляет не только Bot, но и строку bot_claim_state (pg_insert … ON CONFLICT DO NOTHING) — claim воркера выбирает ботов из ledger'а справедливости, бот без строки в нём невидим (сообщение висит queued, воркер молча idle). Регрессионный тест: tests/integration/test_registry_db.py::test_registry_add_makes_bot_claimable. Бэкфилл уже зарегистрированных ботов — INSERT … SELECT WHERE NOT EXISTS (выполнен для gram_mq и gram_mq_test 2026-10-11).
