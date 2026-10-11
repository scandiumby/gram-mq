# Implementation Plan: Управление ботами через CLI (MVP: gram-mq bot add)

**Branch**: `002-bot-management-cli` | **Date**: 2026-10-11 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-bot-management-cli/spec.md`

## Summary

MVP фичи — интерактивный визард `gram-mq bot add`: скрытый ввод токена с
форматной валидацией `{id}:{secret}`, генерация человекочитаемого
bot_slug (последние 4 цифры идентификатора + 2 слова из встроенного
Dune-вордлиста), запись токена только в `bots/.env-<bot_slug>` (0600,
каталог 0700) и атомарная регистрация slug в реестре (таблица `bots` из
фичи 001, схема не меняется). US2 (`bot list`) и FR-013 (блокировка
каталога) отложены в Future Enhancements (Session 2026-10-11). Технический
подход: новый пакет `src/gram_mq/cli/` поверх существующей инфраструктуры
(settings → async-сессия), stdlib argparse + getpass, без новых
зависимостей.

## Technical Context

**Language/Version**: Python 3.12+ (установлено фичей 001).

**Primary Dependencies**: только stdlib для CLI (argparse, getpass,
secrets, pathlib, os); существующие sqlalchemy[asyncio] + asyncpg — для
записи в реестр. Новых зависимостей нет.

**Storage**: реестр ботов — таблица `bots` (фича 001, Alembic-миграция уже
применяется контрактным сюитом); токен — файл `bots/.env-<bot_slug>` в
формате dotenv. Схема БД не меняется — новых миграций нет.

**Testing**: pytest + pytest-asyncio (существующий сюит); быстрые
юнит-тесты генерации slug и валидации токена обязательны (конституция,
Testing); DB-гейты — через `GRAMMQ_TEST_DATABASE_URL` / `TEST_DATABASE_URL`
/ `./.env.test`, без БД — skip.

**Target Platform**: Linux-сервер (выделенный пользователь сервисов);
разработка на macOS/darwin — различий в используемых API нет
(os.geteuid/os.open/getpass POSIX-совместимы).

**Project Type**: CLI-команда в существующем пакете (административный
инструмент, не runtime-код — конституция, Asynchrony).

**Performance Goals**: N/A (интерактивный визард, один оператор;
SC-001 — регистрация за один проход < 2 минут).

**Constraints**: токен не появляется нигде, кроме файла (FR-008): не
логируется, не попадает в тексты ошибок и вывод; ввод скрыт (FR-004);
файл создаётся атомарно (O_EXCL) с правами 0600, каталог — 0700.

**Scale/Scope**: десятки ботов на инсталляцию; коллизии slug статистически
редки (4 цифры + 2 слова из ~150-словного вордлиста), разрешаются
перегенерацией (FR-007, SC-005).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Принцип/стандарт | Статус | Обоснование |
|---|---|---|
| I. Ports and adapters | ✅ | Новый адаптер не добавляется; CLI — административный код, пишет в реестр через существующие модели SQLAlchemy. Изменение схемы реестра не производится. |
| II. At-least-once | ✅ | Не затрагивается (регистрация бота не создает сообщений). |
| III. No plaintext secrets | ✅ | Токен живёт только в `bots/.env-<bot_slug>`; в реестре — только slug; тексты ошибок валидации не содержат токен; файл 0600/каталог 0700 (FR-014); `bots/` в `.gitignore`. |
| IV. Two realms | ✅ | HTTP-поверхность не расширяется (FR-002): маршрут регистрации по HTTP отсутствует. |
| Asynchrony | ✅ | CLI — не runtime-код; тем не менее используется существующая async-инфраструктура (asyncio.run + async_sessionmaker из db.py), синхронных DB-клиентов не появляется. |
| Type checking | ✅ | Новый код проходит `mypy --strict` (гейт `uv run typecheck`). |
| Database migrations | ✅ | Схема не меняется; CREATE/ALTER в коде отсутствуют. |
| Testing | ✅ | Публичные функции покрыты тестами; быстрые тесты slug-генерации и валидации токена — обязательно; суммарное покрытие ≥ 80%; секретов и deploy env в тестах нет. |
| Dependency licenses | ✅ | Новых зависимостей нет. |
| Configuration | ✅ | Единственное новое значение — `GRAMMQ_BOTS_DIR` (не секрет, default `./bots`) в существующем классе `Settings`; CLI читает настройки тем же каналом (fail-fast без `GRAMMQ_DATABASE_URL`). |

Нарушений нет; Complexity Tracking не требуется.

## Project Structure

### Documentation (this feature)

```text
specs/002-bot-management-cli/
├── plan.md              # This file ($speckit-plan command output)
├── research.md          # Phase 0 output ($speckit-plan command)
├── data-model.md        # Phase 1 output ($speckit-plan command)
├── quickstart.md        # Phase 1 output ($speckit-plan command)
├── contracts/
│   └── cli-bot-add.md   # Контракт команды gram-mq bot add
└── tasks.md             # Phase 2 output ($speckit-tasks command)
```

### Source Code (repository root)

```text
src/gram_mq/
├── cli/
│   ├── __init__.py
│   ├── main.py          # argparse: gram-mq bot add; точка входа консоль-скрипта
│   ├── wizard.py        # интерактивный поток визарда (имя → токен → артефакты)
│   ├── token.py         # валидация формата {id}:{secret}, парсинг/запись .env-файла
│   ├── slug.py          # генерация bot_slug (4 цифры + 2 слова Dune)
│   └── dune_words.py    # встроенный Dune-вордлист (константа)
└── settings.py          # + GRAMMQ_BOTS_DIR (default "./bots")

tests/
├── unit/
│   ├── test_slug.py
│   ├── test_token.py
│   └── test_wizard.py
└── integration/
    └── test_registry_db.py   # вставка в реестр; только при TEST_DATABASE_URL
```

**Structure Decision**: single project (defaults фичи 001): `src/`, `tests/`
от корня; новый подпакет `cli/`, новые тестовые каталоги `unit/` и
`integration/` рядом с существующим `contract/`.

## MVP Scope (Session 2026-10-11)

Реализуется только US1: FR-001–FR-010, FR-012, FR-014. US2 (`bot list`,
FR-011, SC-003) и FR-013 (блокировка каталога) — Future Enhancements:
фиксируются задачами пост-MVP фазы в tasks.md и уходят в issues.

## Complexity Tracking

> Не заполняется: нарушений Constitution Check нет.
