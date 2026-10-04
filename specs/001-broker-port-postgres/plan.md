# Implementation Plan: Очередь исходящих сообщений за портом брокера

**Branch**: `001-broker-port-postgres` | **Date**: 2026-09-26 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-broker-port-postgres/spec.md`

## Summary

Доменный контракт очереди `BrokerPort` (enqueue / claim / ack / retry /
dead_letter / queue_depth) с двумя реализациями: `PostgresBroker` на таблице
`messages` (очередь = журнал, лиз + счётчик попыток, справедливый claim
между ботами) и in-memory двойник для юнит-тестов. Единый набор контрактных
тестов прогоняется против обеих реализаций. Схема — миграциями Alembic.

## Technical Context

**Language/Version**: Python 3.12+ (менеджер — uv)

**Primary Dependencies**: SQLAlchemy 2 (async) + asyncpg, Alembic,
pydantic-settings; dev: pytest + pytest-asyncio, ruff, mypy, coverage

**Storage**: PostgreSQL 17 — co-located с сервисами или отдельный инстанс
по TCP (`sslmode=require` при возможности); подключение — по стандарту
Configuration (`GRAMMQ_DATABASE_URL`: SecretStr, GRAMMQ_-префикс, без
дефолта); тестовая БД через `TEST_DATABASE_URL`, без неё Postgres-тесты
пропускаются

**Testing**: pytest + pytest-asyncio, один параметризованный контрактный
сюит против in-memory и PostgresBroker; обязательные гейты — `mypy
--strict` без ошибок и coverage ≥ 80% (Engineering Standards → Type
checking / Testing)

**Target Platform**: Linux-сервер (хостовой Python, без Docker)

**Project Type**: library (внутренний доменный порт + адаптеры будущего
сервиса gram-mq)

**Performance Goals**: сотни/тысячи сообщений в день; постановка O(1)
к глубине очереди; claim — единицы миллисекунд при малой конкуренции

**Constraints**: масштаб v1 — несколько ботов, один-два воркера;
дубли доставки допустимы (at-least-once), потери — нет

**Scale/Scope**: две таблицы, один порт, два адаптера, один контрактный
тест-сюит

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Норма конституции v2.4.0 | Оценка | Комментарий |
| --- | --- | --- |
| I. Ports and adapters | PASS | Контракт в `ports/`, реализации в `adapters/`, домен не знает про SQL; контракт растёт только по необходимости (FR-013) |
| II. At-least-once | PASS | Лиз и attempts обязательны в схеме; ack атомарен (одна транзакция); истёкший лиз возвращается в работу. Пункты про API/worker-процессы и дашборд — N/A для этой фичи |
| III. No plaintext secrets | PASS (N/A) | Фича секретов не касается; в таблице ботов токенов нет по определению |
| IV. Two authorization realms | PASS (N/A) | Фича не expose'ит HTTP-интерфейсов |
| Asynchrony | PASS | Все клиенты async (SQLAlchemy async + asyncpg); в тестах вместо sleep — инжектируемые часы (RQ-8), единственный временной тест допущен сознательно |
| Type checking | PASS | mypy --strict в dev-группе (T001), конфигурация strict в T003, обязательный гейт в T037 |
| Database migrations | PASS | Схема только через Alembic-ревизии (T012); `Base.metadata.create_all()` не применяется |
| Testing | PASS | Контрактный сюит покрывает все публичные операции порта; coverage ≥ 80% — гейт T037; DB-тесты только при TEST_DATABASE_URL; in-memory двойник обязателен |
| Dependency licenses | PASS | Все зависимости MIT-совместимы: SQLAlchemy/Alembic/pydantic/pytest/ruff — MIT, asyncpg — Apache-2.0 |
| Configuration | PASS | Настройки — BaseSettings с префиксом GRAMMQ_: `database_url: SecretStr` (required, без дефолта), `broker_lease_seconds=60`, `broker_max_attempts=5`; env-файл через GRAMMQ_ENV_FILE (RQ-10, T006/T007) |
| Stack and deployment (v1) | PASS | Стек конституционный; Postgres — co-located или отдельный TCP-инстанс |
| Governance (язык артефактов) | PASS | Все артефакты фичи (spec, plan, research, data-model, contracts, quickstart, tasks) на русском — норма v2.4.0; код, идентификаторы и коммиты — английский |

Нарушений нет — Complexity Tracking не заполняется.

**Re-check после Phase 1** (research.md, data-model.md, contracts/,
quickstart.md), обновлено 2026-10-04 по конституции v2.4.0 после правок
по находкам анализа (C2, C3, I1–I4, G1): решения RQ-1…RQ-10 и диаграмма
состояний соответствуют принципам I–II и всем Engineering Standards —
лиз/attempts в схеме, атомарный ack (RQ-9), реклейм истёкшего лиза (RQ-5),
контракт растёт по необходимости, продление лиза вне контракта v1 (RQ-1,
I3), настройки по стандарту Configuration (RQ-10, C3), гейты mypy/coverage
в задачах (C2). Новых нарушений не внесено.

## Project Structure

### Documentation (this feature)

```text
specs/001-broker-port-postgres/
├── plan.md              # This file ($speckit-plan command output)
├── research.md          # Phase 0 output ($speckit-plan command)
├── data-model.md        # Phase 1 output ($speckit-plan command)
├── quickstart.md        # Phase 1 output ($speckit-plan command)
├── contracts/           # Phase 1 output ($speckit-plan command)
│   └── broker-port.md   # Сигнатуры доменного контракта
└── tasks.md             # Phase 2 output ($speckit-tasks — не создаётся тут)
```

### Source Code (repository root)

```text
src/gram_mq/
├── ports/
│   └── broker.py            # BrokerPort, OutboundMessage, Delivery
├── adapters/
│   ├── postgres/
│   │   └── broker.py        # PostgresBroker (SQL, лиз, справедливость)
│   └── memory/
│       └── broker.py        # InMemoryBroker (инжектируемые часы)
├── models/
│   ├── base.py              # DeclarativeBase
│   ├── bots.py              # Bot(bot_slug PK, created_at)
│   └── messages.py          # Message + MessageStatus
├── db.py                    # async engine + async_session
└── settings.py              # pydantic-settings (лиз, лимит попыток, БД)

alembic/
└── versions/                # миграция: bots + messages + индексы

tests/
├── conftest.py              # фикстуры: memory-брокер; postgres-брокер (skipif)
└── contract/
    └── test_broker_port.py  # один сюит, параметризованный по адаптеру
```

**Structure Decision**: структура повторяет целевую из конституции
(ports/adapters/models); фича не создаёт API/worker/CLI — они в последующих
фичах. Референс-реализация — ветка main_copy (`src/gram_mq/ports/broker.py`,
`src/gram_mq/adapters/postgres/broker.py`, `src/gram_mq/adapters/memory/broker.py`).

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

Нарушений конституции нет — секция пуста по правилу шаблона.
