# Quickstart: валидация API и доставки (MVP)

**Date**: 2026-10-11 | Контракты: [http-api.md](contracts/http-api.md),
[telegram-sender.md](contracts/telegram-sender.md)

Сквозной сценарий на живых компонентах: тестовая БД, реальный API-процесс,
реальный бот. Все секреты — синтетика, кроме токена живого бота (§6 —
по желанию).

## 0. Предусловия

```bash
uv sync --group dev
echo 'GRAMMQ_TEST_DATABASE_URL=postgresql+asyncpg://gram:gram@localhost:5432/gram_mq_test' > .env.test
uv run alembic upgrade head        # схема включая gmq_api_keys
uv run pytest                      # весь сюит зелёный
uv run check                       # ruff + format + mypy --strict
```

## 1. Зарегистрировать бота (фича 002)

```bash
GRAMMQ_DATABASE_URL=postgresql+asyncpg://gram:gram@localhost:5432/gram_mq \
  uv run gram-mq bot add
# имя: Test Bot; токен живого бота от BotFather (ввод скрыт)
# → файл bots/.env-<slug>, строка в реестре
```

## 2. Выдать API-ключ

```bash
GRAMMQ_DATABASE_URL=... uv run gram-mq key add
# печатает ключ РОВНО ОДИН РАЗ:
#   gmq_Ab3...                     # сохранить!
docker exec postgres psql -U gram -d gram_mq -t \
  -c "SELECT prefix, left(key_hash, 20) || '...', revoked_at FROM gmq_api_keys;"
# в БД — prefix и Argon2id-хэш; ключа нет
```

## 3. Поднять API и поставить сообщение

```bash
GRAMMQ_DATABASE_URL=... GRAMMQ_BOTS_DIR=$PWD/bots uv run gram-mq-api &
KEY=gmq_...   # из шага 2
curl -s http://127.0.0.1:8080/health                     # {"status":"ok"}
curl -s -X POST http://127.0.0.1:8080/api/v1/messages \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"bot_slug":"<slug>","chat_id":"<ваш chat_id>","text":"quickstart"}'
# → 202 {"id":"<uuid>"}
curl -s -X POST .../api/v1/messages -d '{}' -H 'Content-Type: application/json'
# → 401 (нет ключа); журнал не изменился
curl -s .../api/v1/messages -H "X-API-Key: gmq_wrongwrongwrongwrongwrongwro"
# → 401; с bot_slug "no-such-bot" → 404; text на 4097 символов → 422
```

## 4. Запустить воркер и дождаться доставки

```bash
GRAMMQ_DATABASE_URL=... GRAMMQ_BOTS_DIR=$PWD/bots uv run gram-mq-worker &
# сообщение приходит в чат; в логах воркера — delivered
```

## 5. Подсчитать реально отправленные (US4, SC-002)

```bash
docker exec postgres psql -U gram -d gram_mq_test -t -c \
  "SELECT count(*) FROM gmq_messages WHERE status = 'sent';"
# = число доставленных; недоставленные (queued/leased/failed) не считаются
docker exec postgres psql -U gram -d gram_mq_test -t -c \
  "SELECT bot_slug, count(*) FROM gmq_messages
   WHERE status='sent' GROUP BY bot_slug;"
```

## 6. Живучесть (SC-003): kill между отправкой и подтверждением

Поставить сообщение, у воркера в момент обработки сделать `kill -9` (по
времени — между логами send и ack, проще всего на сообщении с искусственно
замедленным фейком — в автотестах это покрыто детерминированно):
перезапустить воркер — после истечения аренды сообщение доставится
повторно (возможен дубль в чате — допустимо), итоговый статус `sent`.

## 7. Секреты не протекают (SC-004)

```bash
# по логам обоих процессов (journalctl -u gram-mq-api -u gram-mq-worker):
journalctl -u gram-mq-api -u gram-mq-worker --since today | grep -F "$KEY"
journalctl ... | grep -F "<секретная часть токена>"
# оба grep пусты; в БД открытого ключа и токена нет (шаг 2)
```
