# Implementation Plan: Исходящий синхронный прокси Telegram Bot API

**Branch**: `008-telegram-api-proxy` | **Date**: 2026-11-22 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/008-telegram-api-proxy/spec.md`

## Summary

Система получает синхронный исходящий путь к Telegram Bot API рядом с
асинхронной очередью (фичи 001/003, не меняются): маршрут
`POST /api/v1/telegram/{bot_slug}/{method}` внутри машинного realm —
`X-API-Key`, как у всего `/api/v1/*`, новых URL-пространств нет. Хендлер —
чистый сквозной вызов без очередей и ретраев: машинный ключ → политика
методов → токен → тело → Telegram → ответ вызывающему. Политика методов —
константный запретный список `getUpdates/setWebhook/deleteWebhook/logOut/
close` (код, не настройка) плюс белый список по умолчанию с расширением
настройкой `GRAMMQ_TELEGRAM_PROXY_ALLOWED_METHODS` (CSV; базовый дефолт —
`getMe`, `answer*`, `edit*`/`stop*`/`deleteMessage`, `sendChatAction`,
`getChat`/`getChatMember`); запретный список приоритетнее расширения.
Токен собирается существующим `SplitFileBotConfig` (006) в памяти
API-процесса: `NotRegistered` → 404 (единообразный «bot not found»),
`NotReady` → 503. Вызов — новый доменный порт `TelegramMethodCaller`
(сиблинг `TelegramSender` в `ports/telegram.py`: сырые байты тела +
Content-Type на вход, `TelegramMethodResponse(status, content_type,
body)` на выход), адаптер `HttpxTelegramMethodCaller` — POST на
`api.telegram.org/bot<token>/<method>` без логирования URL (токен
редактируется из ошибок). Тело запроса — JSON (проверка парсимости) или
multipart (структурная проверка границ), оба проходят Telegram'у как
есть, без перекодирования (Session 2026-11-22 (2)); лимит тела — 50 МБ
(собственный потолок загрузок Bot API, настройка). Маппинг ответов:
успех Telegram — 200 с телом как есть; ошибки Telegram проходят со своим
кодом и телом (429 — с Retry-After), исключение — 401 от Telegram → 503
(коллизия с нашим 401 авторизации; чинит оператор, не потребитель);
сеть → 502, таймаут → 504. Прокси без состояния: ни миграций, ни таблиц;
журнал — логи `bot + method + исход + длительность`, без тел и токена.
Анти-дубль «одно сообщение — один путь» — контрактом потребителя.

## Technical Context

**Language/Version**: Python 3.12+ (существующий uv-проект).

**Primary Dependencies**: без новых — `httpx>=0.27` уже runtime с 007
(BSD-3-Clause); FastAPI/starlette `Request`/`Response` для сырого
passthrough; aiogram в прокси не участвует (высокоуровневые типизированные
методы не нужны — прокси передает произвольный метод с сырым телом);
существующие SQLAlchemy 2 (async), pydantic-settings.

**Storage**: нет — прокси stateless (решение оператора, Session
2026-11-22): ни миграций, ни таблиц, ни записей в очередях; хранилище
токенов уже существует (006) и только читается.

**Testing**: pytest + pytest-asyncio; эндпоинт — httpx ASGITransport с
fake `TelegramMethodCaller` и fake `BotConfig`; адаптер — httpx
`MockTransport` (сборка URL, побайтовый passthrough, таймаут, редакция
токена из исключений); политика — чистые функции; гейт `uv run check`
(ruff + mypy --strict), покрытие ≥ 80%.

**Target Platform**: тот же api-процесс (контейнер Amvera / systemd
`gram-mq-api.service`) — отдельного процесса и юнита нет; deploy-артефакты
не меняются.

**Project Type**: web-service (расширение машинного HTTP-surface
существующего API).

**Performance Goals**: накладные расходы сверх самого вызова Telegram —
единицы миллисекунд (SELECT половины токена + расшифровка AESGCM двух
половин ~ микросекунды + один POST); укладывается в SC-001 (≤ 1 c).

**Constraints**: никаких ретраев и выжиданий на стороне прокси (FR-002);
токен и тела не логируются, URL `bot<token>` не логируется (III);
очередь исходящих и её гарантии не затрагиваются (II — прокси вне
гарантий доставки, анти-дубль — контракт потребителя); таймаут вызова и
лимит тела — GRAMMQ_-настройки с дефолтами в коде.

**Scale/Scope**: десятки ботов; единицы–десятки синхронных вызовов/мин
(колбэки и правки), пики — вложения до 50 МБ в памяти запроса.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Конституция v3.1.0 (поправка 007 о вебхуках уже внесена). Фича не вводит
новых URL-пространств, процессов и правок схемы — поправок в конституцию
не требуется.

| Принцип/стандарт | Статус | Обоснование |
|---|---|---|
| I. Ports and adapters | ✅ | Новый доменный порт `TelegramMethodCaller` в `ports/telegram.py` (сиблинг `TelegramSender`: тот же файл — та же предметная область Telegram; тип `TelegramMethodResponse`), адаптер `HttpxTelegramMethodCaller` (`adapters/telegram/`), fake в тестах. Переиспользуются существующие порты: `BotConfig` (`worker/bot_config.py` — уже импортируется и CLI, и воркером; API-процесс — третий потребитель, переноса нет) и `KeyVerifier`. API-домен (app.py) зависит только от портов и инъекции; сборка — в `api/main.py`. |
| II. At-least-once (NON-NEGOTIABLE) | ✅ | Прокси — синхронный passthrough, сознательно вне гарантий доставки (спека, Session 2026-11-22): не пишет в очереди, не ретраит, не фиксирует состояние вызовов. Гарантии очереди исходящих (001/003) не затрагиваются ни одной правкой. Границу «ни одного дубля смысла двумя путями» закрывает контракт потребителя «одно сообщение — один путь» (FR-006/FR-007) — система не детектирует нарушение и не обязана (спека, edge case). Повтор вызова потребителем — его ретрай: прокси идемпотентен ровно настолько, насколько идемпотентен сам вызываемый метод Bot API. |
| III. No plaintext secrets | ✅ | Токен существует в открытом виде только в памяти: `SplitFileBotConfig` реассемблирует половины под мастер-ключом (006) на время вызова. Прокси обязан: (а) не логировать URL `api.telegram.org/bot<token>/<method>` — адаптер оборачивает исключения httpx с редакцией токена по образцу `classify(secret=…)` в aiogram-адаптере; (б) не отражать токен в ответах и ошибках; (в) логировать только `bot_slug + method + исход + длительность`, без тел (FR-009). Машинный ключ проверяется существующим `require_api_key` (недифференцированный 401). |
| IV. Two realms (v3.1.0) | ✅ | Прокси — внутри машинного `/api/v1/*`: `X-API-Key`, существующая зависимость `require_api_key`, недифференцированный 401. Вызывающий — машина (внешний сервис), человеческий realm и вебхуки не затрагиваются, новых пространств нет (FR-001). |
| Asynchrony | ✅ | Хендлеры async; `httpx.AsyncClient` (async, typed); чтение тела — `await request.body()`; расшифровка половин токена — микросекундный CPU-bound (прецедент: AESGCM в 006 на пути воркера); никаких `requests`/`time.sleep`. |
| Type checking | ✅ | mypy --strict на всём новом коде; гейт `uv run check`. |
| Database migrations | ✅ | Правок схемы нет — прокси stateless (решение оператора); таблица аудита — Future Enhancements. Реестр ботов и хранилище половин читаются как есть. |
| Testing | ✅ | Эндпоинт — на fakes портов (все коды, порядок проверок: ключ → политика → токен → тело → вызов; отсутствие токена/тел в логах); адаптер — httpx MockTransport без сети; политика методов — чистые функции; интеграционной DB-suite не требуется (нет схемы); реальные секреты и `.env` не используются; покрытие ≥ 80%. |
| Dependency licenses | ✅ | Новых зависимостей нет: httpx (BSD-3-Clause) — runtime с 007; остальное — существующие. |
| Configuration | ✅ | Три новых поля `Settings`: `telegram_proxy_allowed_methods: str` (CSV, несекретный дефолт в коде — базовый перечень), `telegram_proxy_timeout_seconds: float = 30.0`, `telegram_proxy_max_body_bytes: int = 52428800` (50 МБ — собственный потолок загрузок Bot API; оператор может сужить). Запретный список — константа кода, настройкой не переопределяется (FR-004 фиксирован контрактом). `.env.example` расширяется. |
| Stack and deployment (v1) | ✅ | Стек и деплой не меняются: тот же api-процесс, те же юниты/контейнер; `bot_slug` — единый идентификатор; очередь — журнал (прокси её не касается). |

Нарушений нет — Complexity Tracking пуст.

**Re-check после Phase 1 (post-design)**: дизайн-артефакты (research
R1–R10, data-model, contracts, quickstart) сверены с таблицей — все
статусы ✅. Ключевые сверки: (1) passthrough ответов Telegram со своим
кодом — контракт зафиксировал коллизию 401 (наш — авторизация, их —
токен бота) отдельным маппингом 503 (IV, FR-008); (2) сырые байты через
`Request`/`Response` без pydantic-схем — тела не логируются, токен
редактируется (III); (3) белый список — настройка с дефолтом в коде,
запретный список — константа, приоритет зафиксирован в чистой функции
политики (Configuration, FR-004/FR-005); (4) ни одна правка не касается
очередей и воркеров (II). Гейт пройден, ERROR нет.

## Project Structure

### Documentation (this feature)

```text
specs/008-telegram-api-proxy/
├── plan.md                       # This file ($speckit-plan command output)
├── research.md                   # Phase 0 output ($speckit-plan command)
├── data-model.md                 # Phase 1 output ($speckit-plan command)
├── quickstart.md                 # Phase 1 output ($speckit-plan command)
├── contracts/
│   └── telegram-api-proxy.md     # POST /api/v1/telegram/{bot_slug}/{method}:
│                                 #   машинный контракт + анти-дубль потребителя
└── tasks.md                      # Phase 2 output ($speckit-tasks command)
```

### Source Code (repository root)

```text
src/gram_mq/
├── ports/
│   └── telegram.py               # + TelegramMethodCaller (Protocol),
│                                  #   TelegramMethodResponse(status, content_type, body)
├── adapters/
│   └── telegram/
│       └── httpx_method_caller.py   # NEW: HttpxTelegramMethodCaller — POST сырых байтов,
│                                     #      shared AsyncClient, timeout из настройки,
│                                     #      редакция токена в исключениях, aclose()
├── api/
│   ├── app.py                    # + BANNED_TELEGRAM_METHODS (константа),
│   │                             #   resolve_method_policy() (чистая функция),
│   │                             #   POST /api/v1/telegram/{bot_slug}/{method} —
│   │                             #   регистрируется только при инъекции обоих портов;
│   │                             #   маппинг кодов, журналирование без тел/токена
│   └── main.py                   # + сборка: SplitFileBotConfig (registry.fetch_half) +
│                                 #   HttpxTelegramMethodCaller + парсинг CSV перечня
└── settings.py                   # + 3 поля (см. Configuration)

.env.example                      # + GRAMMQ_TELEGRAM_PROXY_* с окнами применения
README.md                         # + секция синхронного прокси

tests/
├── unit/
│   ├── test_telegram_proxy_endpoint.py   # NEW: коды/порядок/артефакты/секреты (fakes)
│   └── test_httpx_method_caller.py       # NEW: MockTransport — URL, passthrough,
│                                          #      таймаут/сеть, Retry-After, редакция
```

**Structure Decision**: расширение существующих файлов вместо новых
пакетов: порт ложится в `ports/telegram.py` (одна предметная область —
один файл контракта, как `ports/broker.py` для исходящих), адаптер —
сиблинг aiogram-адаптера в `adapters/telegram/`, эндпоинт и сборка — в
существующих `api/app.py`/`api/main.py` (паттерн условной регистрации по
инъекции портов — как `/webhooks/*` в 007: без инъекции обоих портов всё
`/api/v1/telegram/*` остаётся 404, существующие тесты create_app без
новых аргументов не меняются). Отдельный процесс/пакет не нужен: прокси —
часть api-процесса, ждёт только Telegram (не внешний сервер и не БД,
кроме одного SELECT половины токена).
