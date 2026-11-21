# Quickstart: проверка фичи 006 вручную

**Spec**: [spec.md](spec.md) | **Контракты**:
[bot-bootstrap-endpoint.md](contracts/bot-bootstrap-endpoint.md),
[token-storage.md](contracts/token-storage.md)

Сквозная проверка на локальной машине: создание бота через окно бутстрапа,
доставка со склеиванием половин, ревок, реакция на подмену. Команды —
через `uv run` из корня репозитория.

## Предусловия

- Python 3.12+, uv; Postgres (локальный или тестовый).
- Переменные окружения (или `./.env`): `GRAMMQ_DATABASE_URL`,
  `GRAMMQ_TOKEN_MASTER_KEY` — сгенерировать:
  `python -c "import os; print(os.urandom(32).hex())"`.
- Для проверки реальной отправки — тестовый бот BotFather и chat_id; без
  них шаги отправки проверяются до «воркер попытался отправить» (лог).

## 1. Миграция и запуск

```bash
uv run migrate                                   # alembic upgrade head
uv run gram-mq-api &                             # API
GRAMMQ_BOT_BOOTSTRAP_ENABLED=true \
GRAMMQ_BOT_BOOTSTRAP_TOKEN=<токен из BotFather> \
  uv run gram-mq-api &                           # (вариант: env в .env)
uv run gram-mq-worker &
```

Окно открыто, если переменные заданы до старта API-процесса.

## 2. Создание бота (US1)

```bash
curl -s -X POST localhost:8080/bootstrap/bots \
  -H 'Content-Type: application/json' -d '{"name": "Test Bot"}'
# ожидание: 201 {"status":"created","bot_slug":"1234-word-word"}
```

Проверки:

- `ls -l bots/` — файл `.env-<slug>` с правами 0600; внутри — имя,
  hex-блобы, **нет** plaintext-токена;
- в БД: `SELECT bot_slug, pair_nonce FROM gmq_bots;` — nonce совпадает со
  значением `GRAMMQ_BOT_PAIR_NONCE` из файла;
- повторный POST с тем же токеном → 409;
- при выключенном флаге (рестарт без `BOT_BOOTSTRAP_ENABLED`) → 404.

## 3. Доставка (US2)

```bash
curl -s -X POST localhost:8080/api/v1/messages \
  -H "X-API-Key: <ключ key add/prepare>" -H 'Content-Type: application/json' \
  -d '{"bot_slug": "<slug из шага 2>", "chat_id": "<chat>", "text": "hi"}'
# 202; воркер в логах: delivered id=... telegram_message_id=...
```

Отрицательные проверки:

- подменить один байт в `GRAMMQ_BOT_FILE_HALF` файла → следующая отправка
  ретраится с причиной «bot config unreadable (file/keys)»; к Telegram
  воркер не обращается; вернуть байт — доставка восстанавливается;
- удалить строку из `gmq_bots` (имитация ревока) при живой очереди →
  сообщение сразу в dead-letter: `status='failed'`,
  `error='bot not registered'`.

## 4. Ревок (US3)

```bash
curl -s -X DELETE localhost:8080/bootstrap/bots/<slug>
# ожидание: 200 {"status":"revoked","bot_slug":"<slug>"}
```

Проверки: строки нет в `gmq_bots`; файла нет; строка
`gmq_bot_claim_state` **остаётся** — леджер переживает ревок (так
очередь бота доходит до воркера и уходит в dead-letter), это штатно;
`POST /api/v1/messages` к slug → 404; повторный DELETE → 404.

## 5. Безопасность хранения (SC-002/SC-003)

- `grep -r "<фрагмент секретной части токена>" bots/` — пусто (в файле
  только шифртекст);
- в дампе `gmq_bots` нет `id:`-префикса и фрагментов секрета;
- логи API и воркера не содержат токена и половин.

## 6. Закрытие окна

`GRAMMQ_BOT_BOOTSTRAP_ENABLED=false`, `GRAMMQ_BOT_BOOTSTRAP_TOKEN=` —
редеплой. Мастер-ключ остаётся; его потеря = все боты «не готовы» навсегда
(восстановление — перевыпуск токенов в BotFather).
