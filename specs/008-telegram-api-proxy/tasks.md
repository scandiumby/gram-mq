---
description: "Task list for feature 008 implementation"
---

# Tasks: Исходящий синхронный прокси Telegram Bot API

**Input**: Design documents from `/specs/008-telegram-api-proxy/`

**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/telegram-api-proxy.md, quickstart.md

**Tests**: включены как обязательные — стандарт Testing конституции (каждый публичный метод покрыт, ≥ 80%, fake/MockTransport без сети и секретов).

**Organization**: задачи сгруппированы по user stories (US1–US4 спеки); чистые утилиты, порт и адаптер — в foundational: их использует каждая история.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: параллелизуемо (разные файлы, нет зависимостей от незавершённых задач)
- **[Story]**: US1–US4 из spec.md
- Путь файла — в каждой задаче

## Path Conventions

Один проект: `src/`, `tests/` от корня репозитория (uv).

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: канал настроек фичи — до любого кода (Configuration)

- [x] T001 Добавить три поля в `Settings` (src/gram_mq/settings.py): `telegram_proxy_allowed_methods: str = DEFAULT_TELEGRAM_PROXY_ALLOWED_METHODS` (CSV-константа базового перечня из research R4, 13 методов: getMe, answerCallbackQuery, answerInlineQuery, answerWebAppQuery, sendChatAction, editMessageText, editMessageCaption, editMessageReplyMarkup, editMessageLiveLocation, stopMessageLiveLocation, deleteMessage, getChat, getChatMember), `telegram_proxy_timeout_seconds: float = 30.0`, `telegram_proxy_max_body_bytes: int = 52_428_800`. Все несекретные, с комментарием-окном применения (по образцу полей 007)
- [x] T002 [P] Добавить три переменные `GRAMMQ_TELEGRAM_PROXY_ALLOWED_METHODS / _TIMEOUT_SECONDS / _MAX_BODY_BYTES` с комментариями в .env.example (дефолты, «пусто/мусор CSV — ошибка старта», 50 МБ = потолок загрузок Bot API)

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: порт, адаптер и чистые функции политики — используют все истории

- [x] T003 Добавить в src/gram_mq/ports/telegram.py (сиблинг TelegramSender): frozen-slots dataclass `TelegramMethodResponse(status: int, content_type: str, body: bytes)`; исключение `TelegramCallError(kind: Literal["network", "timeout"])`; `TelegramMethodCaller` Protocol `async def call_method(*, bot_token: str, method: str, content_type: str, body: bytes) -> TelegramMethodResponse` (сигнатуры — дословно data-model.md; инвариант: объект существует ⇔ Telegram дал HTTP-ответ)
- [x] T004 Реализовать `HttpxTelegramMethodCaller` в src/gram_mq/adapters/telegram/httpx_method_caller.py (depends T003): общий `httpx.AsyncClient` на процесс (`timeout=httpx.Timeout(...)` из параметра, `follow_redirects=False`), POST `https://api.telegram.org/bot{token}/{method}` с исходными `Content-Type` и байтами тела без перекодирования; маппинг исключений: `httpx.TimeoutException` → `TelegramCallError("timeout")`, прочие `httpx.HTTPError`/`OSError` → `TelegramCallError("network")`; перед подъёмом — редакция токена `str(exc).replace(token, "<redacted>")` (образец: `classify(secret=…)` в aiogram_sender.py); `aclose()` закрывает клиент
- [x] T005 Тесты адаптера в tests/unit/test_httpx_method_caller.py (depends T004): httpx MockTransport — URL содержит токен и имя метода; тело и заголовки проходят побайтово (JSON и multipart); ответ → TelegramMethodResponse как есть; заголовок `Retry-After` сохраняется; таймаут → kind="timeout", обрыв → kind="network"; текст исключения не содержит токен (редакция)
- [x] T006 [P] Чистые функции политики в src/gram_mq/api/app.py: константа `BANNED_TELEGRAM_METHODS = {"getUpdates", "setWebhook", "deleteWebhook", "logOut", "close"}` (настройкой не переоткрывается, FR-004); `resolve_method_policy(method: str, allowed: frozenset[str]) -> Literal["ok", "banned", "not_allowed"]` — запрещённый список проверяется первым, сравнение регистронезависимое; `parse_allowed_methods(csv: str) -> frozenset[str]` — trim, нижний регистр, элементы `^[a-z]+$` после нормализации; пустая строка целиком или любой мусорный элемент → ValueError (fail-fast, research R9)

**Checkpoint**: фундамент готов — порт/адаптер тестируются без сети, политика — без HTTP.

## Phase 3: User Story 1 - Синхронный вызов метода Bot API (Priority: P1) 🎯 MVP

**Goal**: сквозной вызов `POST /api/v1/telegram/{bot_slug}/{method}` с ответом Telegram в том же запросе

**Independent Test**: getMe через прокси с машинным ключом → 200 с обёрткой Telegram; в gmq_messages/gmq_inbound_updates — ни одной новой строки (quickstart §1 шаг 4)

### Tests for User Story 1

- [x] T007 [P] [US1] Тесты эндпоинта (часть 1) в tests/unit/test_telegram_proxy_endpoint.py: fake `BotConfig` (Ready/NotRegistered/NotReady) + fake `TelegramMethodCaller` (запрограммированный ответ/исключение), httpx ASGITransport. Матрица из contracts/telegram-api-proxy.md: 200 — тело/Content-Type Telegram побайтово; 401 без/с неверным ключом до вызова порта; 404 неизвестный бот и метод вне `^[A-Za-z]{1,64}$`; 503 NotReady без вызова порта; 415 не-JSON/не-multipart; 422 непарсящийся JSON и multipart без завершающей границы `--<boundary>--`; ошибки Telegram проходят своим кодом и телом (400/403/404/500); 429 + заголовок Retry-After; 401 от Telegram → 503 с исходным телом; TelegramCallError timeout → 504, network → 502; все `{"detail": …}` gram-mq — статические литералы. Тесты должны падать до реализации (T008)

### Implementation for User Story 1

- [x] T008 [US1] Эндпоинт в src/gram_mq/api/app.py (depends T007, использует T003/T004-интерфейс и T006): `create_app(..., bot_config: BotConfig | None = None, method_caller: TelegramMethodCaller | None = None, allowed_methods: frozenset[str] = frozenset())` — маршрут `POST /api/v1/telegram/{bot_slug}/{method}` регистрируется только при обоих портах (иначе 404, паттерн /webhooks/* из 007); зависимости: существующий `require_api_key`; порядок проверок: ключ → регулярка метода (404) → политика (403 `method not allowed`, единый для banned/not_allowed) → `token_for` (NotRegistered → 404 `bot not found`, NotReady → 503 `bot not ready`) → Content-Type (415) → `Content-Length`/длина после чтения > лимита (413) → json.loads для JSON-тел / структурная проверка multipart (422) → `call_method` → маппинг ответа по таблице R5 (passthrough статуса/тела/Content-Type; 429 + Retry-After; Telegram-401 → 503; TelegramCallError → 502/504). Хендлер без тел в логах и без интерполяции исключений в detail
- [x] T009 [US1] Сборка в src/gram_mq/api/main.py (depends T008): `SplitFileBotConfig(bots_dir=Path(settings.bots_dir), fetch_half=SqlAlchemyBotRegistry(get_session_factory()).fetch_half, master_key=parse_master_key(settings.token_master_key))` + `HttpxTelegramMethodCaller(timeout_seconds=settings.telegram_proxy_timeout_seconds)` + `parse_allowed_methods(settings.telegram_proxy_allowed_methods)` (ValueError — падение старта с ясным сообщением) + передача тройки в create_app; агент-выключение клиента вместе с процессом (lifespan/close — по образцу aclose у sender'а воркера)

**Checkpoint**: US1 работает независимо: MVP-прогон quickstart §1 (шаги 3–5) зелёный.

---

## Phase 4: User Story 2 - Токен не покидает стабильный сервер (Priority: P1)

**Goal**: сквозная гарантия — токен отсутствует в ответах, журнале и ошибках на всех путях

**Independent Test**: полный прогон матрицы из T007+T010 с assertions отсутствия токена (US2 спеки, сценарий 1)

### Tests for User Story 2

- [x] T010 [P] [US2] Тесты отсутствия секрета в tests/unit/test_telegram_proxy_endpoint.py (расширение, depends T008): (a) `caplog` по всей матрице — записи логгера gram_mq.api не содержат токен, значение X-API-Key и тела запросов/ответов; (b) каждый `{"detail": …}` на путях отказов — статическая строка без исключений; (c) тест-двойник порта поднимает исключение с токеном в тексте → 502/504 detail не отражает текст исключения; (d) паспорт допущения: passthrough-тела Telegram токеном не сканируются (Telegram не эхает токены; побайтовая идентичность FR-002) — зафиксировать комментарием теста

### Implementation for User Story 2

- [x] T011 [US2] Пройти красные места T010 в src/gram_mq/api/app.py и src/gram_mq/adapters/telegram/httpx_method_caller.py: любые найденные интерполяции исключений/деталей заменить статическими литералами; инвариант III — комментарий-обоснование у хендлера (как у require_api_key). Ожидается близко к нулю правок — задача страховочная: при зелёном T010 закрывается сразу

**Checkpoint**: US2 — свойство подтверждено тестами на всех путях.

---

## Phase 5: User Story 3 - Контроль методов (Priority: P2)

**Goal**: запретный список и белый перечень с расширением настройкой, запретный приоритетнее

**Independent Test**: quickstart §2 — getUpdates → 403 при любом перечне; sendPhoto → 403 до расширения, 200 после добавления в настройку

### Tests for User Story 3

- [x] T012 [P] [US3] Тесты политики в tests/unit/test_telegram_proxy_endpoint.py (расширение, depends T006/T008): `resolve_method_policy` — таблица (banned ∩ allowed → "banned"; ok; not_allowed); `parse_allowed_methods` — нормализация, мусор → ValueError, пусто → ValueError; эндпоинт: каждый из 5 запрещённых → 403 без вызова порта; метод вне перечня → 403, тело единообразно `method not allowed` (без различения уровня); расширение перечня (передать другой frozenset в create_app) открывает метод, запрещённый остаётся 403

### Implementation for User Story 3

- [x] T013 [US3] Включить политику в путь эндпоинта src/gram_mq/api/app.py (depends T012): вызов `resolve_method_policy` после auth до `token_for`; оба отказа — один 403 (детали — contracts/telegram-api-proxy.md, раздел «Политика методов»); отказ логируется с reason=method_not_allowed (структура — в T014)

**Checkpoint**: US3 закрыт: default-deny работает, приоритет запретного списка — тестом.

---

## Phase 6: User Story 4 - Эксплуатация и настройка (Priority: P3)

**Goal**: журнал вызовов, таймаут и лимит тела — настроены и наблюдаемы

**Independent Test**: серия вызовов (успех/отказ/запрещён) видна в логах как bot+method+status+dur; таймаут и лимит меняются без кода (quickstart §2 последние строки)

- [x] T014 [US4] Журналирование в src/gram_mq/api/app.py (depends T013): одна строка на вызов — успех: `logger.info("proxy bot=%s method=%s status=%s dur=%sms", …)` (dur — `time.monotonic()` вокруг вызова порта); отказы до Telegram: `logger.warning("proxy denied … bot=%s method=%s reason=%s", …)` с reason ∈ {auth, method_not_allowed, not_found, not_ready, bad_body, too_large}; никогда — токен, тела, ключ (US2/T010 держат инвариант)
- [x] T015 [US4] Лимит тела в src/gram_mq/api/app.py + тесты в tests/unit/test_telegram_proxy_endpoint.py: 413 `request body too large` по `Content-Length` до чтения и по фактической длине после чтения (chunked без Content-Length); лимит — параметр сборки из `settings.telegram_proxy_max_body_bytes` (T009 уже передаёт)
- [x] T016 [P] [US4] Проверка настройки без правки кода по quickstart §2 (строки «Расширение перечня», «Лимит скорости», «Токен не готов»): задокументированный ручной прогон curl'ом; результаты — в quickstart §4

**Checkpoint**: US4 закрыт: журнал наблюдаем, обе настройки применяются.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T017 [P] Секция прокси в README.md: маршрут, оба перечня методов, ссылка на контракты потребителя (анти-дубль «одно сообщение — один путь», повторы на стороне потребителя) — по contracts/telegram-api-proxy.md
- [x] T018 Гейты: `uv run check` (ruff + mypy --strict) и `uv run coverage run -m pytest -q` — зелёно, покрытие ≥ 80%; сводка в quickstart §4 (прогон реализатора)
- [x] T019 Прогнать quickstart.md (§0 автоматизированный, §1–§2 ручной/curl) и заполнить раздел §4 «Результат прогона»

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: без зависимостей; T001 → всё остальное (настройки читает сборка)
- **Foundational (Phase 2)**: T003 → T004 → T005 (порт → адаптер → его тесты); T006 параллелен всей цепочке
- **User Stories (Phases 3–6)**: строго последовательно P1 → P1 → P2 → P3 — все расширяют один хендлер (api/app.py) и один файл тестов; внутри фазы тесты раньше реализации (TDD)
- **Polish (Phase 7)**: после всех историй

### User Story Dependencies

- **US1**: после Phase 2 — единственная внешняя зависимость (порт/адаптер/политика-функции)
- **US2**: после US1 (свойства путей, которые строит US1)
- **US3**: после US1 (включение политики в готовый путь)
- **US4**: после US3 (журнал фиксирует reason-коды политики)

### Parallel Opportunities

- T001 ∥ T002; T006 ∥ (T003 → T004 → T005); внутри US1: T007 пишется параллельно завершению Phase 2
- T008/T009 последовательны (endpooint → сборка), но T009 ∥ T010 (разные файлы)
- T012 ∥ T011 (тест-файл vs возможные правки адаптера); T016 ∥ T017

### Parallel Example: Phase 2

```bash
# Одна линия:
Task: "T003 Порт в src/gram_mq/ports/telegram.py"
Task: "T004 Адаптер в src/gram_mq/adapters/telegram/httpx_method_caller.py"
Task: "T005 Тесты адаптера в tests/unit/test_httpx_method_caller.py"
# Параллельно ей, другой исполнитель:
Task: "T006 Чистые функции политики в src/gram_mq/api/app.py"
```

---

## Implementation Strategy

### MVP First

MVP этой фичи = **US1 + US3** (фазы 3 и 5): синхронный вызов + действующий шлюз политики. Голый US1 без US3 открывает все методы Bot API — против операторского решения default-deny; до кластера US3 внешний выпуск не планировать. US2 (проверка секретности) — до любого внешнего выпуска обязательно.

### Incremental Delivery

1. Phase 1–2 → фундамент с зелёными тестами адаптера и политики
2. Phase 3 (US1) → сквозной вызов, quickstart §1
3. Phase 4 (US2) → гарантия секретности
4. Phase 5 (US3) → шлюз методов; **первые безопасные внешние демонстрации**
5. Phase 6 (US4) → эксплуатация
6. Phase 7 → документация, гейты, quickstart-валидация

---

## Notes

- [P] = разные файлы, нет зависимостей; внутри US порядок тесты → реализация (TDD)
- Правки чужих фич отсутствуют by design: очередь (001/003), вебхуки (007), хранилище токенов (006) не трогаются — инвариант SC-004 проверяется в T007/T019
- Ни одного нового процесса, URL-пространства и миграции — план пройден без правок конституции
- Ревью всего фичи целиком перед коммитом; один коммит после аппрува

## Phase 8: Convergence

*(добавлено $speckit-converge 2026-11-22; сверка кода против спеки/плана/задач/конституции)*

- [x] T020 Добавить тест бюджета времени хендлера прокси в tests/unit/test_telegram_proxy_endpoint.py: ≥50 итераций полного пути (fake TelegramMethodCaller, максимальный cost проверок — политика+токен+валидация тела) укладываются в 1 c каждая на вложение системы, по образцу TestResponseTimeBudget из tests/unit/test_webhook_endpoint.py per SC-001 (partial)
- [x] T021 Выполнить ручную валидацию применения настроек quickstart §2 (расширить GRAMMQ_TELEGRAM_PROXY_ALLOWED_METHODS, перезапустить api, curl добавить метод/запрещённый метод) и зафиксировать результат в quickstart §4, либо явно перенести эти строки §2 в §3 как эксплуатационные per T016 (partial)
- [x] T022 Вынести дефолт лимита тела в именованную константу DEFAULT_TELEGRAM_PROXY_MAX_BODY_BYTES в src/gram_mq/settings.py и переиспользовать её как дефолт параметра proxy_max_body_bytes в src/gram_mq/api/app.py (сейчас литерал 52_428_800 продублирован в двух местах) per plan R9 (partial)
