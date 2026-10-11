# Quickstart: валидация gram-mq bot add (MVP)

**Date**: 2026-10-11 | Контракт команды: [contracts/cli-bot-add.md](contracts/cli-bot-add.md)

Проверяется сквозной сценарий US1 на тестовом окружении. Реальный токен
не используется — достаточно токена в валидном формате.

## 0. Предусловия

```bash
uv sync --group dev
# тестовая БД (без неё DB-шаги пропустятся как skip):
echo 'GRAMMQ_TEST_DATABASE_URL=postgresql+asyncpg://gram:gram@localhost:5432/gram_mq_test' > .env.test
```

Полный гейт: `uv run check` (ruff + format + mypy --strict) и
`uv run pytest` — зелёные.

## 1. Регистрация бота

```bash
mkdir -p bots && chmod 700 bots
GRAMMQ_DATABASE_URL=postgresql+asyncpg://gram:gram@localhost:5432/gram_mq \
  uv run gram-mq bot add
# Имя бота: Test Bot
# Токен бота ({id}:{secret}): 123456789:AAHtest-secret-value   # ввод скрыт
```

Ожидание:

- итог показывает имя, slug вида `6789-<слово1>-<слово2>` и путь файла —
  токена в выводе нет;
- `bots/.env-<slug>` существует, права `-rw-------` (0600), содержимое —
  две строки `GRAMMQ_BOT_TOKEN=...` и `GRAMMQ_BOT_NAME=Test Bot`;
- реестр получил строку:

```bash
psql "$GRAMMQ_DATABASE_URL" -c "SELECT bot_slug, created_at FROM gmq_bots;"
```

## 2. Дубль токена отклоняется (FR-012)

Повторный запуск с тем же токеном `123456789:...` → понятная ошибка
дубля, exit 3; новых файлов и строк не появилось:

```bash
ls bots/ | wc -l          # не изменилось
psql ... -c "SELECT count(*) FROM gmq_bots;"   # не изменилось
```

## 3. Отмена не оставляет артефактов (FR-009)

Запуск, ввод имени, затем Ctrl+C на любом шаге → «Регистрация отменена»,
exit 130, файлов и строк не прибавилось.

## 4. Секрет не протекает (SC-002, выборочная проверка)

```bash
# вывод команды не содержит секретную часть:
GRAMMQ_DATABASE_URL=... uv run gram-mq bot add 2>&1 | tee /tmp/wizard.out
! grep -F 'AAHtest-secret-value' /tmp/wizard.out   # grep ничего не находит → true
```

## 5. Ошибки формата и владелец каталога

- Токен `123456` (без `:`) и `123:abc:def` (два `:`) → ошибка формата,
  повтор prompt, артефактов нет.
- `sudo chown root:root bots && gram-mq bot add` → отказ «каталог
  принадлежит другому пользователю» до начала ввода, exit 2 (проверять на
  VM/контейнере; на личной машине пропустить).
