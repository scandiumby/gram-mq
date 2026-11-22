# Quickstart: Валидация фичи 008 (синхронный прокси Telegram Bot API)

**Date**: 2026-11-22 | **Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)

Руководство проверки, не реализации: детали контрактов — в
[contracts/telegram-api-proxy.md](contracts/telegram-api-proxy.md),
объекты и настройки — в [data-model.md](data-model.md). Все команды —
от корня репозитория через `uv`.

## 0. Автоматизированная проверка (гейты)

```bash
uv sync --group dev
uv run check                        # ruff + mypy --strict
uv run pytest tests/unit/test_telegram_proxy_endpoint.py -q
uv run pytest tests/unit/test_httpx_method_caller.py -q
uv run test                         # вся suite (регресс: существующие фичи не задеты)
```

Ожидается: зелёно; покрытие ≥ 80% (`uv run coverage report`).
DB-гейтированных тестов фича не добавляет (схема не меняется);
прогон против реального Postgres остаётся инвариантом остальных фич.

Проверяемые инварианты автоматически: все коды и порядок проверок
эндпоинта (ключ → политика → токен → тело → вызов —
[telegram-api-proxy.md](contracts/telegram-api-proxy.md)); маппинг
ответов (passthrough кодов/тел Telegram, 401→503, сеть/таймаут →
502/504); политика (запретный список приоритетнее расширения; 415/413/
422); побайтовый passthrough тела и Retry-After адаптером (MockTransport);
редакция токена из исключений; отсутствие токена и тел в логах.

## 1. Сквозной прогон вручную (одна машина)

Пререквизиты: Postgres, зарегистрированный бот (bootstrap-окно фичи
006), `GRAMMQ_TOKEN_MASTER_KEY` в env-файле. Реальный Telegram не
обязателен: валидируются контракт и коды; на шаге 5 при желании —
живой вызов.

```bash
# 1. Схема актуальна (новых миграций нет — upgrade идемпотентен)
uv run alembic upgrade head

# 2. Настройки: обязательных для фичи нет (дефолты в коде); опционально
#    GRAMMQ_TELEGRAM_PROXY_TIMEOUT_SECONDS=30
#    GRAMMQ_TELEGRAM_PROXY_MAX_BODY_BYTES=52428800
#    GRAMMQ_TELEGRAM_PROXY_ALLOWED_METHODS=getMe,answerCallbackQuery,...

# 3. Запустить API (порт из GRAMMQ_API_PORT) и получить ключ оператора
#    (gram-mq key add / prepare — фича 005)
uv run gram-mq-api &

# 4. getMe: успех — 200 с обёрткой Telegram как есть
curl -s -X POST http://127.0.0.1:8080/api/v1/telegram/<bot_slug>/getMe \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{}'
# Ожидание: {"ok":true,"result":{"id":…,"first_name":…}} — ответ Telegram,
# очередь сообщений не тронута (SELECT gmq_messages — без изменений).

# 5. (опционально, живой Telegram) правка сообщения, отправленного очередью:
curl -s -X POST http://127.0.0.1:8080/api/v1/telegram/<bot_slug>/editMessageText \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"chat_id":"<chat>","message_id":<id>,"text":"edited via proxy"}'
# Ожидание: {"ok":true,"result":true}; в чате — правка, не новое сообщение
# (анти-дубль: правка через прокси поверх доставки очередью — штатно).
```

## 2. Политика методов и отказы (главные сценарии спеки)

Ожидания — из [telegram-api-proxy.md](contracts/telegram-api-proxy.md):

| Сценарий | Действие | Ожидание |
|---|---|---|
| Без ключа | curl без X-API-Key | 401 `{"detail":"unauthorized"}`; обращения к Telegram нет |
| Запрещённый метод | POST …/getUpdates | 403 `method not allowed`; у Telegram состояние вебхуков не тронуто |
| Вне перечня | POST …/sendPhoto | 403 `method not allowed` |
| Неизвестный бот | POST …/no-such-bot/getMe | 404 `bot not found` |
| Битый метод в пути | POST …/get-Me/../x | 404 (регулярка `^[A-Za-z]{1,64}$`) |
| Битое тело | Content-Type: application/json, `-d '{'` | 422; обращения к Telegram нет |
| Чужой Content-Type | `-H 'Content-Type: text/plain'` | 415 |
| Ошибка Telegram | editMessageText с несуществующим message_id | код Telegram (400) и его тело как есть |
| Сеть до Telegram | закрыть исходящий доступ / указать недоступный endpoint тестом | 502/504 `{"detail":…}` — наша форма тела, не Telegram'овская |
| Дубль вызова потребителем | повторить curl из шага 4 дважды | оба выполняются независимо (passthrough); идемпотентность — свойство метода Bot API |
| Ничего в очередях | SELECT по gmq_messages / gmq_inbound_updates после любой серии | изменений нет — прокси stateless (SC-004) |

*(Сценарии, требующие смены настройки с перезапуском процесса или
живого Telegram, перенесены в §3 — решение оператора по T021.)*

## 3. Что НЕ проверяется этим руководством

Живые загрузки файлов через multipart (проверяются адаптерными тестами
на MockTransport + при желании вручную sendPhoto с файлом после
расширения перечня); поведение под нагрузкой; таблица аудита вызовов
(Future Enhancements); контракт потребителя на его стороне (анти-дубль
и его повторы — обязанность внешнего сервиса, документирована
контрактом); сценарии, требующие смены настройки с перезапуском
процесса или живого Telegram (перенесены из §2 решением оператора,
T021; автоматические эквиваленты — в §4):

- «Расширение перечня» / «Расширение на запретный»: смена
  `GRAMMQ_TELEGRAM_PROXY_ALLOWED_METHODS` и перезапуск api —
  эксплуатационный шаг деплоя; поведение доказано тестом
  `TestPolicy.test_whitelist_opens_but_banned_stays_closed`;
- «Лимит скорости»: настоящий 429 от Telegram — нужен живый вызов;
  маппинг и Retry-After покрыты адаптерными тестами;
- «Токен не готов»: удаление `bots/.env-<slug>` на живом инстансе —
  эксплуатационный шаг; поведение покрыто `TestToken` (503 без вызова
  порта).

## 4. Результат прогона (implement, 2026-11-22)

Решение оператора по T021 (convergence): сценарии §2, требующие смены
настройки/перезапуска или живого Telegram, перенесены в §3 как
эксплуатационные; их автоматические эквиваленты перечислены ниже.

Автоматизированная часть (§0 и эквиваленты §1–2 в тестах):

- `uv run check` — 3/3 гейта (ruff check, ruff format, mypy --strict
  на src+tests+alembic), 0 ошибок.
- `uv run coverage run -m pytest -q` — **324 passed** (+50 фичи: 12
  адаптер + 38 эндпоинт, включая бюджетный тест времени SC-001 из
  convergence; память + реальный Postgres через `./.env.test`),
  покрытие **84%** ≥ 80%. Новые модули: адаптер httpx — 100%,
  `api/app.py` — 97%, `ports/telegram.py` — 100%.
- Сценарии §2 покрыты тестами: все коды/порядок проверок/passthrough/
  ремаппинг 401→503/Retry-After — `tests/unit/test_telegram_proxy_endpoint.py`;
  «Расширение перечня» и «приоритет запретного списка» — там же
  (TestPolicy.test_whitelist_opens_but_banned_stays_closed,
  TestParseAllowedMethods); «Лимит скорости» и транспортные отказы —
  адаптерными тестами Retry-After/timeout/network; «Токен не готов» —
  TestToken; секретность (US2) — TestNoSecretLeaks (caplog по всей
  матрице: токен/ключ/тела отсутствуют в записях).
- Смоук сборки: `create_app` без портов прокси держит `/api/v1/telegram/*`
  404 (TestSurfaceControl — обратная совместимость чужих suite);
  `parse_allowed_methods` мусор/пусто — ValueError на старте (fail-fast).

Ручные шаги с живым Telegram (§1 шаг 5, §2 строки «живых» загрузок)
остаются эксплуатационными — см. §3. Правок схемы нет: `alembic
upgrade head` идемпотентен, DB-suite остальных фич зелёная (323 passed
включает контрактные PG-прогоны 001/007).
