# Implementation Plan: Шифрованное распределённое хранение токена бота

**Branch**: `006-encrypted-token-storage` | **Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/006-encrypted-token-storage/spec.md`

## Summary

Токен бота перестаёт существовать в plaintext: при создании через
бутстрап-эндпоинт (окно, выключенное по умолчанию, полный токен — из
секретной настройки) он разрезается на две неполные части — файловую
(`id:secret[:4]`) и табличную (`secret[4:]`) — и каждая шифруется AESGCM
под выделенным мастер-ключом `GRAMMQ_TOKEN_MASTER_KEY`; AAD обеих половин —
случайный 8-байтовый nonce парности, хранимый открыто при каждой половине.
Slug генерируется системой по действующим правилам (`<last4>-<word>-<word>`
от числового id токена) и возвращается в ответе — это единственный «id
бота»; ревок — `DELETE /bootstrap/bots/{slug}` под тем же флагом: строка
реестра, claim-state и файловая половина удаляются, очередь бота уходит в
dead-letter. Воркер при каждой доставке читает обе половины, расшифровывает,
проверяет nonce парности и склеивает токен в памяти; рассинхрон или подмена
падает локально до обращения к Telegram. CLI-визард и plaintext-формат
файлов выпиливаются. Схема: `gmq_bots` получает `token_half`/`pair_nonce`,
FK `gmq_messages → gmq_bots` снимается (журнал переживает ревок), проверка
существования бота при enqueue становится явной.

## Technical Context

**Language/Version**: Python 3.12+ (существующий uv-проект).

**Primary Dependencies**: +`cryptography` (AESGCM; Apache-2.0 OR
BSD-3-Clause — совместимо со стандартом лицензий); остальное существующее:
FastAPI, SQLAlchemy 2 (async) + asyncpg + Alembic, pydantic-settings,
aiogram 3, argon2-cffi.

**Storage**: PostgreSQL — `gmq_bots` расширяется (`token_half` Text NULL,
`pair_nonce` String(16) NULL) миграцией `0003`; FK
`fk_gmq_messages_bot_slug_gmq_bots` снимается в той же ревизии. Файлы
`bots/.env-<slug>` — новый шифрованный формат.

**Testing**: pytest + pytest-asyncio; httpx ASGITransport для эндпоинтов,
fake-реестр вместо БД, tmp-каталог вместо `bots/`; интеграционные тесты
против `GRAMMQ_TEST_DATABASE_URL` (миграция + жизненный цикл); `uv run
check` (ruff + mypy --strict).

**Target Platform**: Linux-сервер/PaaS-контейнер (Amvera: один контейнер
api+worker, том `/data/bots`, переменные панели); systemd-деплой — те же
переменные в едином env-файле.

**Project Type**: web-service (API + worker) с административными
бутстрап-эндпоинтами.

**Performance Goals**: расшифровка двух половин — единицы микросекунд
(CPU, AES-NI); на фоне сетевого вызова Telegram и двух round-trip Postgres
доля < 0,1% (SC-004). Создание бота — O(число файлов в `bots/`) на дедуп:
расшифровка всех файловых половин (ожидаемые десятки ботов — миллисекунды).

**Constraints**: plaintext токена — только в секретной настройке на окно
операции и кратко в памяти процесса (FR-013); в HTTP, логах, ответах, БД и
файлах — никогда; оба эндпоинта при дефолтных настройках недоступны
(FR-001/SC-002); права файла 0600 и каталога 0700 сохраняются (FR-006).

**Scale/Scope**: один оператор; единичные создания/ротации/ревоки; десятки
ботов; доставка — сотни сообщений/мин.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Конституция v3.0.0 (правка 2026-10-19, проведена этой фичой заранее —
см. FR-018 спеки) уже содержит допуски, под которые проектируется фича:
шифрованные неполные половины в Principle III, бутстрап-эндпоинт управления
ботами в Principle IV, оконный сеттинг полного токена в Configuration.

| Принцип/стандарт | Статус | Обоснование |
|---|---|---|
| I. Ports and adapters | ✅ | Домен API получает инъектируемые порты: `BotProvisioner` (создание: slug + запись половин) и `BotRevoker` (ревок) + снапшот `BotManagement(enabled, token)` — по образцу `KeyBootstrap`/`KeyActivator` из 005. Порт `BotConfig` воркера эволюционирует: `token_for` возвращает типизированный результат `TokenLookup` (Ready/NotRegistered/NotReady) — новый член порта появляется под нужду конкретного адаптера (различение «бот удалён» и «конфиг временно нечитаем»), спека это покрывает (FR-012/FR-015). Криптография — чистый модуль `token_store.py` без технологий; DB-адаптер реестра — `adapters/postgres/bot_registry.py`; ни один адаптер не «проезжает» в домен. |
| II. At-least-once (NON-EGOTIABLE) | ✅ | Сообщения не теряются: ревок переводит оставшуюся очередь в dead-letter с причиной (не молчаливое удаление); `attempts`/lease-механика не меняется; enqueue по-прежнему валидирует бота (после снятия FK — явным SELECT'ом, семантика `UnknownBotError` → 404 сохранена); краш между отправкой и ack закрыт lease-реклеймом как раньше. |
| III. No plaintext secrets (v3.0.0) | ✅ | В БД — Argon2id-хэши ключей и шифрованная табличная половина токена (единственное исключение, внесённое правкой v3.0.0); в файле — шифрованная файловая половина; plaintext — только в `GRAMMQ_BOT_BOOTSTRAP_TOKEN` на окно операции и кратко в памяти; ответ создания — факт + slug; логи без половин и nonce. |
| IV. Two realms (v3.0.0) | ✅ | URL-пространства realm-ов не расширяются: `POST/DELETE /bootstrap/bots` — тот же третий административный surface, что `/bootstrap/api-key` из 005: без машинной авторизации, по умолчанию 404, активен в окно операции, тело без секретов (только имя). Регистрация ботов по HTTP теперь допустима правкой v3.0.0 (эндпоинт выключен по умолчанию, токен — из настройки, не из запроса). |
| Asynchrony | ✅ | Хендлеры async; AESGCM/чтение файлов — микросекундный CPU-bound без сетевого IO (прецедент: Argon2 в `ApiKeyVerifier`); файловые операции создания — `asyncio.to_thread` не нужны (микро-объём), DB — SQLAlchemy async. |
| Type checking | ✅ | mypy --strict на всём новом коде; гейт `uv run check`. |
| Database migrations | ✅ | Одна ревизия Alembic `0003`: add columns `gmq_bots.token_half/pair_nonce`, drop FK `gmq_messages→gmq_bots`; схема меняется только через миграцию, `alembic upgrade head` идемпотентно выполняется на старте контейнера. |
| Testing | ✅ | Чистые функции `token_store.py` (split/encrypt/decrypt/AAD/форматы) — unit; эндпоинты — httpx + fakes (404/201/409/400/503, отсутствие секретов в ответах); воркер-маппинг TokenLookup → retry/DL; интеграционные: миграция, жизненный цикл бота в PG, enqueue после снятия FK; секретов и deploy-env в тестах нет; покрытие ≥80%. |
| Dependency licenses | ✅ | `cryptography` — двойная лицензия Apache-2.0 OR BSD-3-Clause, обе в разрешённом списке стандарта; собранные wheels под manylinux/slim без системных пакетов. |
| Configuration | ✅ | Три новых поля `Settings` через `GRAMMQ_`-канал: `bot_bootstrap_enabled: bool = False` (дефолт верен во всех окружениях), `bot_bootstrap_token: SecretStr \| None = None` (оконный секрет — допуск v3.0.0), `token_master_key: SecretStr \| None = None` (секрет, отдельный от всех прочих); `repr()` не раскрывает; читаются на старте, смена — редеплой. |
| Stack and deployment (v1) | ✅ | Стек не меняется; контейнер Amvera и systemd-юниты — те же (новые переменные через панель/env-файл); `bots/` остаётся volume, не попадает в образ. |

Нарушений нет — Complexity Tracking пуст.

**Re-check после Phase 1 (post-design)**: дизайн-артефакты (research R1–R13,
data-model, contracts, quickstart) перепроверены против таблицы — статусы
не изменились, все ✅. Ключевые сверки: (1) снятие FK + явная проверка
реестра в `enqueue` сохраняет контракт `UnknownBotError` → 404 (II);
(2) `TokenLookup.NotRegistered` → немедленный dead-letter с причиной —
«ни одно сообщение не исчезает молча» (II); (3) файловая запись в
асинхронном хендлере — микро-объёмные операции без сетевого IO,
прецедент CPU-bound Argon2 в `ApiKeyVerifier` (Asynchrony); (4)
`cryptography` Apache-2.0 OR BSD-3 — разрешённый список (Dependency
licenses). Гейт пройден, ERROR нет.

## Project Structure

### Documentation (this feature)

```text
specs/006-encrypted-token-storage/
├── plan.md                       # This file ($speckit-plan command output)
├── research.md                   # Phase 0 output ($speckit-plan command)
├── data-model.md                 # Phase 1 output ($speckit-plan command)
├── quickstart.md                 # Phase 1 output ($speckit-plan command)
├── contracts/
│   ├── bot-bootstrap-endpoint.md # POST/DELETE /bootstrap/bots + настройки окна
│   └── token-storage.md          # Разрез/шифрование/формат файла/склейка в воркере
└── tasks.md                      # Phase 2 output ($speckit-tasks command)
```

### Source Code (repository root)

```text
src/gram_mq/
├── token_store.py            # NEW: чистое ядро — split токена, AESGCM-половины,
│                             #      nonce парности, формат файла, парсинг мастер-ключа
├── slug.py                   # MOVED из cli/slug.py (+ dune_words.py рядом): генерация slug
├── settings.py               # + bot_bootstrap_enabled, bot_bootstrap_token,
│                             #   token_master_key
├── models/
│   └── bots.py               # + token_half: Text | None, pair_nonce: String(16) | None
├── adapters/postgres/
│   ├── broker.py             # enqueue: явная проверка реестра вместо FK-IntegrityError
│   └── bot_registry.py       # NEW: add/fetch/delete бота (+ claim_state),
│                             #      дедуп-скан файловых половин (наследник SqlAlchemyBotRegistry)
├── api/
│   ├── app.py                # + BotManagement, POST /bootstrap/bots, DELETE /bootstrap/bots/{slug}
│   ├── main.py               # + сборка: provisioner/revoker, мастер-ключ, bots_dir
│   └── schemas.py            # + BotCreateIn(name)
├── worker/
│   ├── bot_config.py         # порт: TokenLookup (Ready/NotRegistered/NotReady);
│   │                         # адаптер SplitFileBotConfig(bots_dir, session_factory, master_key)
│   ├── runner.py             # NotRegistered → permanent dead_letter «bot not registered»;
│   │                         # NotReady → retryable с отличимой причиной
│   └── main.py               # сборка SplitFileBotConfig
└── cli/
    ├── main.py               # − bot add / bot list (визард выпилен); key add/prepare остаются
    ├── wizard.py             # DELETED
    └── token.py              # DELETED (валидация формата и файловый формат — в token_store.py)

alembic/versions/20261019_0003_encrypted_bot_tokens.py  # NEW (см. data-model.md)

.env.example                  # + GRAMMQ_BOT_BOOTSTRAP_ENABLED / _TOKEN / GRAMMQ_TOKEN_MASTER_KEY
bots/.env-<slug>              # формат файла меняется: шифрованная половина + nonce (contracts)

tests/unit/
├── test_token_store.py       # NEW: split/AESGCM/AAD-подмена/формат файла/мастер-ключ
├── test_slug.py              # MOVED из test валидации cli (правила не меняются)
├── test_bot_bootstrap.py     # NEW: эндпоинты (404/201/409/400/503), секретов в ответах нет
└── test_runner.py            # + маппинг TokenLookup (DL/ретрай/отличимость причин)
tests/integration/
└── test_bot_lifecycle_db.py  # NEW: миграция 0003, enqueue после снятия FK, ревок с очередью
```

**Structure Decision**: расширение существующей структуры без новых пакетов.
Чистое крипто-ядро — отдельным модулем `token_store.py` (образец — чистые
примитивы `api/auth.py` из 005): и API (шифрует при создании), и воркер
(расшифровывает при доставке) зависят от него, а не друг от друга.

> **Уточнение 2026-11-21 (implement, T024)**: интеграционный прогон вскрыл
> дефект R9 — удаление строки `gmq_bot_claim_state` при ревоке делало
> очередь бота незахватываемой навсегда (claim итерирует леджер, не
> журнал). Исправление: миграция `0003` снимает и FK
> `gmq_bot_claim_state → gmq_bots`; ревок удаляет только строку бота и
> файл, строка леджера, как и журнал, переживает бота, а очередь
> доходит до воркера и уходит в dead-letter штатно. Той же датой:
> невалидное имя в `POST /bootstrap/bots` даёт 422 (pydantic-схема,
> единообразно с `/api/v1`), а не 400 — контракт обновлён.
Генерация slug переезжает из `cli/` в `src/gram_mq/slug.py`: единственный
потребитель теперь API, а CLI-пакет после выпиливания визарда не тянет
мёртвый код. DB-операции реестра — в `adapters/postgres/bot_registry.py`
(наследник `SqlAlchemyBotRegistry` визарда, включая посев `bot_claim_state`
— контракт фичи 001); логика портовой подписи `BotConfig` остаётся в
`worker/bot_config.py` (порт + адаптер там же, как сейчас). Порядок записи
при создании — файл, затем строка реестра (прецедент FR-009 визарда):
частичный сбой лечится повторным POST (дедуп-скан находит «файл без строки»
и завершает пару под прежним slug — FR-009 спеки).

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

Нарушений конституции нет (правка v3.0.0 проведена до планирования);
таблица пуста.
