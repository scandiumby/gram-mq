# Quickstart: Валидация фичи 007 (входящие вебхуки → очередь → внешний сервер)

**Date**: 2026-11-21 | **Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)

Руководство проверки, не реализации: детали контрактов — в
[contracts/](contracts/), схема — в [data-model.md](data-model.md).
Все команды — от корня репозитория через `uv`.

## 0. Автоматизированная проверка (гейты)

```bash
uv sync --group dev
uv run check                        # ruff + mypy --strict
uv run pytest tests/contract -q     # контракт InboundBrokerPort на InMemoryInboundBroker
uv run test                         # вся быстрая suite (эндпоинт на fakes, runner, store, CLI)
```

Ожидается: зелёно; покрытие ≥ 80% (`uv run coverage report`).
DB-гейтированное (миграция 0004, жизненный цикл в Postgres) — при
заполненном `./.env.test` (`GRAMMQ_TEST_DATABASE_URL`), иначе skip —
инвариант проекта, не пропуск фичи.

Проверяемые инвариенты автоматически: коды эндпоинта
([webhook-endpoint.md](contracts/webhook-endpoint.md)); атомарность ack и
реклейм lease ([inbound-broker.md](contracts/inbound-broker.md));
маппинг ошибок доставки и экспоненциальный backoff
([update-deliverer.md](contracts/update-deliverer.md)); порядок
Telegram→БД и exit-коды CLI ([webhook-admin-cli.md](contracts/webhook-admin-cli.md));
секрет не появляется в ответах/логах/БД открыто.

## 1. Сквозной прогон вручную (одна машина)

Пререквизиты: Postgres (например, docker), зарегистрированный бот
(bootstrap-окно фичи 006), публичный HTTPS-адрес не нужен — эндпоинт
дергается curl'ом от имени Telegram.

```bash
# 1. Схема
uv run alembic upgrade head        # применяет 0004: gmq_inbound_updates и др.

# 2. Настройки (./.env или GRAMMQ_ENV_FILE): обязательны для фичи
#    GRAMMQ_TOKEN_MASTER_KEY=...        (уже есть — боты шифрованы им)
#    GRAMMQ_WEBHOOK_PUBLIC_URL=https://example.com   (для CLI set)
#    GRAMMQ_WEBHOOK_MASTER_KEY=<hex 32 байт>
#    GRAMMQ_INBOUND_DELIVERY_URL=http://127.0.0.1:9009/updates
#    GRAMMQ_INBOUND_DELIVERY_AUTH_HEADER_VALUE=...   (опционально)

# 3. Приём включён: секрет сгенерирован и передан Telegram (здесь — real
#    set_webhook; для локальной проверки без Telegram шаг можно сымитировать
#    прямой записью HMAC в gmq_bots, см. контракт webhook-admin-cli)
uv run gram-mq webhook set <bot_slug>

# 4. Запустить API (порт из GRAMMQ_API_PORT) и внешний сервер-приёмник
uv run gram-mq-api &
python -m http.server 9009 &          # любой приёмник POST; 200/503 по сценарию

# 5. Апдейт «от Telegram». Секрет система не печатает (по дизайну), для
#    локального прогона выберите своё значение секрета, вычислите
#    проверочное значение и положите его в строку бота (формат —
#    contracts/webhook-admin-cli.md):
SECRET=local-test-secret
KEY_HEX=<hex GRAMMQ_WEBHOOK_MASTER_KEY, 64 символа>
HMAC=$(uv run python -c "from gram_mq.webhook_store import webhook_secret_hmac; print(webhook_secret_hmac(bytes.fromhex('$KEY_HEX'), '$SECRET'))")
psql "$GRAMMQ_DATABASE_URL" -c "UPDATE gmq_bots SET webhook_secret_hmac = '$HMAC' WHERE bot_slug = '<bot_slug>';"
curl -i -X POST http://127.0.0.1:8080/webhooks/<bot_slug> \
  -H "X-Telegram-Bot-Api-Secret-Token: $SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"update_id": 1, "message": {"chat": {"id": 42}, "text": "hi"}}'
# Ожидание: 200; строка queued в gmq_inbound_updates (psql).

# 6. Доставляющий воркер
uv run gram-mq-inbound-worker        # отдельный процесс (US2)
# Ожидание: в логе — deliver; строка стала delivered (delivered_at заполнен);
# приёмник получил тот же JSON (SC-003).
```

## 2. Отказоустойчивость (главные сценарии спеки)

Повторить шаг 5/6 в вариантах; ожидания — из
[webhook-endpoint.md](contracts/webhook-endpoint.md) и
[inbound-broker.md](contracts/inbound-broker.md):

| Сценарий | Действие | Ожидание |
|---|---|---|
| Неверный секрет | curl без заголовка / с мусором | 401; строк в журнале не прибавилось |
| Неизвестный бот | curl на `/webhooks/no-such` | 404; артефактов нет |
| Битое тело | `{"message": …}` без update_id | 422; артефактов нет |
| Ретрай Telegram | повторить curl шага 5 байт-в-байт | 200; записей не прибавилось (поглощение мягким дедупом) |
| Коллизия update_id | тот же update_id, другое тело | 200; новая запись — это новый апдейт, доставляется |
| Хранилище лежит | остановить Postgres перед curl | 503 (не 200!) — Telegram повторит; потерь нет |
| Внешний сервер лежит | погасить приёмник, послать апдейты | приём (шаг 5) не деградирует; воркер повторяет с растущей задержкой (лог: retry … in Ns, N удваивается с капом 300 c); после max_attempts — failed с причиной |
| Восстановление | поднять приёмник до исчерпания попыток | все накопленные доставлены без ручного вмешательства (SC-004) |
| Переживание рестарта | перезапустить api и inbound-worker при непустой очереди | строки на месте, доставка продолжается (SC-001) |
| 4xx от приёмника | приёмник отвечает 400 | апдейт сразу failed, повторов нет (FR-006) |
| Выключение приёма | `uv run gram-mq webhook delete <slug>` | Telegram deleteWebhook; curl с прежним секретом → 404 |

## 3. Что НЕ проверяется этим руководством

Реальные setWebhook/deleteWebhook против Telegram (нужны публичный HTTPS
и живой бот — эксплуатационный шаг после деплоя); поведение прокси/TLS
(внешняя инфраструктура, Assumptions спеки); пер-бот маршрутизация и
фильтрация типов апдейтов (Future Enhancements).

## 4. Результат прогона (implement, 2026-11-22)

Автоматизированная часть (§0 и эквиваленты §1–2 в тестах):

- `uv run check` — 3/3 гейта (ruff check + format, mypy --strict), 0
  ошибок.
- `uv run coverage run -m pytest -q` — **274 passed** (память + реальный
  Postgres через `./.env.test`), покрытие **83%** ≥ 80%.
- Сценарии §1–2 покрыты тестами: приём/коды/артефакты —
  `tests/unit/test_webhook_endpoint.py` + сквозной путь над Postgres в
  `tests/integration/test_inbound_db.py`; «Ретрай Telegram» (поглощение)
  и «Коллизия update_id» — unit + контрактная suite (memory+PG); lying
  приёмник → экспоненциальный backoff / 4xx → failed —
  `tests/unit/test_inbound_runner.py`; переживание рестарта — контрактная
  suite (общий стор/одна БД, два экземпляра брокера); CLI и
  порядок Telegram→БД — `tests/unit/test_webhook_cli.py`.
- Смоук: `gram-mq webhook set --help` парсится; `python -m
  gram_mq.inbound_worker` без `GRAMMQ_INBOUND_DELIVERY_URL` падает на
  старте с сообщением (fail-fast).
- Миграция `0004` применена прогоном suite на чистом обновлении тестовой
  БД (`alembic upgrade head` из conftest), объекты схемы проверены
  запросами information_schema.

Ручные шаги с живым Telegram (§1 шаг 3, §2 строка «Ретрай Telegram»
через настоящий повтор) остаются эксплуатационными — см. §3.
