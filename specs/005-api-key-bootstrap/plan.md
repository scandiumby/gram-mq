# Implementation Plan: Двухэтапный бутстрап API-ключа

**Branch**: `005-api-key-bootstrap` | **Date**: 2026-10-17 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/005-api-key-bootstrap/spec.md`

## Summary

Выдача API-ключа для деплоев без shell (Amvera) в два этапа без ослабления
конституционного инварианта «plaintext не пересекает HTTP». Этап 1 — новая
локальная команда `gram-mq key prepare`: генерирует ключ (существующий
формат `gmq_` + 43 симв.), печатает plaintext один раз и одно готовое к
копированию значение активации `prefix:хэш` (Argon2id, как в действующей
верификации); команда синхронная, чистая, не инстанцирует `Settings` и не
трогает БД. Этап 2 — `POST /bootstrap/api-key`: маршрут вне обоих realm-ов,
по умолчанию выключен флагом `GRAMMQ_KEY_BOOTSTRAP_ENABLED` (выкл.),
материал приходит только через настройку `GRAMMQ_KEY_BOOTSTRAP_MATERIAL`
(SecretStr), из запроса секретов не приходит вовсе; эндпоинт парсит
материал, вставляет prefix + хэш в существующую таблицу `gmq_api_keys`
через injectable-порт `KeyActivator`, конфликт префикса — 409. Новых
таблиц, миграций и зависимостей нет; Argon2 на стороне сервера в этом
пути не выполняется вовсе (хэш предвычислен локально).

## Technical Context

**Language/Version**: Python 3.12+ (существующий uv-проект).

**Primary Dependencies**: новых нет — FastAPI, argon2-cffi, SQLAlchemy 2
(async), pydantic-settings уже в проекте (фичи 003/001).

**Storage**: PostgreSQL — только существующая таблица `gmq_api_keys`
(prefix PK, key_hash, created_at, revoked_at); новых миграций нет.

**Testing**: pytest + pytest-asyncio; httpx ASGITransport для эндпоинта,
FakeActivator вместо БД; дешёвый PasswordHasher (t=1, m=64 KiB) в тестах
хэширования; `uv run check` (ruff + mypy --strict).

**Target Platform**: Linux-сервер/PaaS-контейнер (Amvera: один контейнер
api+worker через `deploy/run_all.py`, порт 80); генерация — локальная
машина оператора с uv.

**Project Type**: web-service + дополнение CLI.

**Performance Goals**: активация — один INSERT, O(1); Argon2id в пути
активации на сервере не выполняется (хэш предвычислен на этапе 1) —
CPU-bound работы в запросе нет вообще.

**Constraints**: plaintext ключ не появляется в HTTP, настройках сервера,
БД и логах (FR-014/FR-009); ответ активации — только факт + prefix; при
дефолтных настройках эндпоинт недоступен (FR-007/SC-002); существующий
`key add` не меняется (FR-016).

**Scale/Scope**: один оператор, редкие активации (окно операции);
ротация/отзыв ключей — существующие механизмы (`revoked_at`, новый ключ).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Принцип/стандарт | Статус | Обоснование |
|---|---|---|
| I. Ports and adapters | ✅ | Домен (`api/app.py`) получает порт `KeyActivator` (Protocol: `activate(prefix, key_hash) -> bool`) и снапшот `KeyBootstrap(enabled, material)`; DB-адаптер активации собирается в `api/main.py` рядом с `fetch` верификатора. CLI `key prepare` — не runtime-код, переиспользует примитивы `api/auth.py` (прецедент: `key add`). Новый адаптер появился через Spec Kit (эта спека). |
| II. At-least-once (NON-NEGOTIABLE) | ✅ | Доставку не касается: эндпоинт пишет только в `gmq_api_keys`; журнал сообщений и воркер не затронуты. |
| III. No plaintext secrets | ⚠️→✅ с трактовкой | Инварианты сохранены: в БД — только Argon2id-хэш (существующая колонка); plaintext печатается один раз в терминале и не пересекает HTTP ни на одном этапе; через настройки проходит только хэш (= то, что и так лежит в БД); ответ эндпоинта — факт + prefix, без хэша. Отклонение от буквы «ключ выдаётся только CLI на сервере»: CLI теперь запускается локально у оператора, регистрация на сервере — через выключаемый по умолчанию эндпоинт, принимающий только хэш. Обоснование — см. Complexity Tracking; опционально уточняется PATCH-амендмантом формулировки. |
| IV. Two realms | ✅ с обоснованием | URL-пространства realm-ов не расширяются: маршрут `POST /bootstrap/api-key` не входит ни в `/api/v1/*` (машинная авторизация невозможна принципиально — «курица и яйцо», FR-013), ни в `/api/dashboard/*`. Это третий, административный surface: без аутентификации, по умолчанию выключен, активен только в окно операции, ничего не отдаёт кроме факта и prefix. Саморегистрации нет: посторонний не может создать пригодный ключ (plaintext ему неизвестен, материал берётся из настройки, а не из запроса — US3). Регистрация ботов по HTTP по-прежнему запрещена. |
| Asynchrony | ✅ | Хендлер async; активация — SQLAlchemy async; Argon2 в рантайм-пути отсутствует (предвычислен локально), `to_thread` не нужен. CLI — не runtime-код. |
| Type checking | ✅ | mypy --strict на всём новом коде; гейт `uv run check`. |
| Database migrations | ✅ | Схема не меняется — новых ревизий нет; используется существующая `gmq_api_keys` (спека FR-008). |
| Testing | ✅ | Публичные функции покрыты: `format/parse_key_material`, `run_key_prepare`, эндпоинт (404/201/409/503, отсутствие хэша в ответах), CLI-диспетчер; FakeActivator вместо БД; секретов и deploy-env в тестах нет; покрытие ≥80%. |
| Dependency licenses | ✅ | Новых зависимостей нет. |
| Configuration | ✅ | Два новых поля `Settings`: `key_bootstrap_enabled: bool = False` (не секрет; дефолт «выключено» верен во всех окружениях — версируется в коде) и `key_bootstrap_material: SecretStr \| None = None` (секретное, без дефолтного значения; `repr()` не раскрывает). Оба через `GRAMMQ_`-канал и общий env-файл; читаются на старте (assumption спеки). CLI `key prepare` `Settings` не инстанцирует (FR-002). |
| Stack and deployment | ✅ | uv-CLI, контейнер Amvera не меняется (новые переменные — через панель); systemd-деплой — те же env-переменные в едином env-файле. |

Единственное отклонение (буква III) обосновано в Complexity Tracking;
ошибок гейта нет.

## Project Structure

### Documentation (this feature)

```text
specs/005-api-key-bootstrap/
├── plan.md                       # This file ($speckit-plan command output)
├── research.md                   # Phase 0 output ($speckit-plan command)
├── data-model.md                 # Phase 1 output ($speckit-plan command)
├── quickstart.md                 # Phase 1 output ($speckit-plan command)
├── contracts/
│   ├── cli-key-prepare.md        # Контракт команды `gram-mq key prepare`
│   └── key-bootstrap-endpoint.md # Контракт POST /bootstrap/api-key + настройки
└── tasks.md                      # Phase 2 output ($speckit-tasks command)
```

### Source Code (repository root)

```text
src/gram_mq/
├── api/
│   ├── app.py             # + KeyActivator Protocol, KeyBootstrap, POST /bootstrap/api-key
│   ├── auth.py            # + PREFIX_RE, format_key_material(), parse_key_material()
│   └── main.py            # + сборка: KeyBootstrap из Settings, DB-адаптер KeyActivator
├── cli/
│   ├── keys.py            # + run_key_prepare() — sync, чистая, без БД
│   └── main.py            # + подкоманда `key prepare` (парсер, вывод, без Settings)
└── settings.py            # + key_bootstrap_enabled, key_bootstrap_material

.env.example               # + документация двух новых переменных

tests/unit/
├── test_keys_cli.py       # + сценарии key prepare (материал, различие, верифицируемость)
└── test_key_bootstrap.py  # NEW: format/parse материала + эндпоинт (404/201/409/503)
```

**Structure Decision**: расширение существующей структуры фич 002/003
без новых пакетов: примитивы формата ключа — рядом с действующими в
`api/auth.py`; порт и маршрут — в `api/app.py`; сборка адаптера — в
`api/main.py` (прецедент `fetch`); CLI — в существующих `cli/keys.py` и
`cli/main.py`. Миграций нет.

> **Уточнение 2026-10-18 (converge T016)**: порт `KeyActivator` реализован
> как коллэйбл-алиас `Callable[[str, str], Awaitable[bool]]`, а не
> Protocol-класс с методом `.activate`, как указано выше в дереве и в R5:
> живой прогон quickstart (T014) вскрыл дефект сборки (замыкание из
> `api/main.py` не имело `.activate`), коллэйбл следует образцу
> `KeyFetcher`/`fetch` в `api/auth.py`. Семантика порта — инъекция в
> `create_app`, контракт True/False, сборка DB-адаптера в `api/main.py` —
> без изменений.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Буква III: «ключ выдаётся только CLI на сервере» → CLI локально + эндпоинт активации хэша | На контейнерных деплоях (Amvera) shell и доступ к БД отсутствуют: единственный путь администрирования — переменные окружения панели и HTTP. Без этого бутстрап первого ключа на таком деплое невозможен вовсе | (а) Проброс БД наружу и `key add` с laptops — открывает БД в сеть, грубое нарушение принципа наименьшего доступа. (б) Зашитый в образ ключ — статичный секрет в артефакте деплоя, прямо противоречит III. (в) Постоянный админ-эндпоинт с паролем — расширяет поверхность атаки навсегда ради одной операции; выбранное решение открывает surface только на окно операции, принимает только хэш (не plaintext) и по умолчанию выключено. Инварианты III (в БД только хэши; plaintext один раз в терминале; никогда по HTTP) не нарушены ни на одном этапе |
