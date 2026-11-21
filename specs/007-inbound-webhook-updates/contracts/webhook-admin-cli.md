# Contract: CLI управления вебхуками

**Spec**: FR-007, FR-008; US3 | **Research**: R2, R7, R12

Команды пакета `gram-mq webhook` (`src/gram_mq/cli/webhook.py`); CLI — не
runtime-код (конституция Asynchrony/Configuration), но настройки читает
тем же `Settings`-каналом. Exit-коды — конвенция CLI: 0 ok, 2 предусловия,
4 сбой записи.

## `gram-mq webhook set <bot_slug>`

Предусловия (нарушение → exit 2, без побочных эффектов):

- бот есть в реестре, обе половины токена читаются и расшифровываются
  (`SplitFileBotConfig`: NotRegistered / NotReady → exit 2);
- задан `GRAMMQ_WEBHOOK_PUBLIC_URL` (база публичного HTTPS-адреса);
- задан валидный `GRAMMQ_WEBHOOK_MASTER_KEY`.

Последовательность:

1. сгенерировать секрет: `secrets.token_urlsafe(32)` — alphabet
   `[A-Za-z0-9_-]`, 43 символа (допустимо для `secret_token` Telegram);
2. `set_webhook(url = f"{public}/webhooks/{slug}", secret_token=secret)`
   через aiogram `Bot` от собранного токена (сетевой вызов; `allowed_updates`
   и `drop_pending_updates` не задаём — дефолты Telegram, всё проходит,
   ничего не теряется);
3. после подтверждения Telegram — записать в строку бота
   `webhook_secret_hmac = hex(HMAC-SHA256(master_key, secret))`
   (сбой записи → exit 4: Telegram уже знает новый секрет, эндпоинт будет
   401 до перезапуска команды).

Порядок «Telegram → БД» — у сознательно безопасного отказа: значение в
БД появляется только когда Telegram его уже знает. Повтор команды
идемпотентен: новый секрет, новый set_webhook, перезапись колонки.

Вывод: slug, полный адрес вебхука, статус. Секрет НЕ печатается, НЕ
логируется, в БД не попадает в открытом виде (FR-008); plaintext живёт
только в памяти процесса CLI в окно операции (конституция III).

## `gram-mq webhook delete <bot_slug>`

1. `delete_webhook()` (drop_pending_updates не задаём — дефолт False);
2. `webhook_secret_hmac = NULL` в строке бота (сбой записи → exit 4).

После удаления: Telegram не шлёт апдейты, прямые запросы на
`/webhooks/{slug}` с прежним секретом → 404 (приём выключен, см.
[webhook-endpoint.md](webhook-endpoint.md)).

## Ревок бота (существующий `DELETE /bootstrap/bots/{slug}`, фича 006)

Удаляет строку реестра — вместе с `webhook_secret_hmac`: приём гасится
автоматически. Оперативно снять вебхук у Telegram при ревоке нечем
(токен уже недоступен) — остаточные апдейты получат 404; строка бота в
Telegram продолжит числить вебхук до `deleteWebhook`/ротации токена —
эксплуатационная заметка, не контракт системы. Очередь и доставку ревок
не касается (журнал переживает бота, US-спеки edge).

## Проверочное значение секрета (общее с эндпоинтом)

Чистый модуль `src/gram_mq/webhook_store.py`:

- `make_webhook_secret() -> str` — генерация;
- `webhook_secret_hmac(master_key: bytes, secret: str) -> str` — hex
  HMAC-SHA256 (то, что хранится в `gmq_bots.webhook_secret_hmac`);
- `verify_webhook_secret(master_key: bytes, stored_hex: str, presented: str
  | None) -> bool` — constant-time (`hmac.compare_digest` дайджестов);
  presented None → False без вычислений.
