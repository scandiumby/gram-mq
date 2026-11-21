# Quickstart: валидация двухэтапного бутстрапа API-ключа

**Date**: 2026-10-17 | Контракты: [cli-key-prepare.md](contracts/cli-key-prepare.md),
[key-bootstrap-endpoint.md](contracts/key-bootstrap-endpoint.md) | Модель:
[data-model.md](data-model.md)

Сквозной сценарий на живых компонентах: локальная генерация без БД →
активация на сервере → ключ работает → окно закрыто, ключ жив. Всё,
кроме тестовой БД, — синтетика; сетевой доступ не нужен.

## 0. Предусловия

```bash
uv sync --group dev
uv run pytest        # весь сюит зелёный (включая новые test_key_bootstrap)
uv run check         # ruff + format + mypy --strict
# тестовая БД (как в 003): .env.test с GRAMMQ_TEST_DATABASE_URL
uv run alembic upgrade head   # схема включая gmq_api_keys (миграций фичи нет)
```

## 1. Этап 1 — локальная генерация (без БД и настроек)

```bash
unset GRAMMQ_DATABASE_URL        # ключевая проверка FR-002
uv run gram-mq key prepare
```

Ожидание: код 0; вывод содержит plaintext ключ один раз, `prefix:` и
строку `Значение активации ...: gmq_xxxxxxxx:$argon2id$...`. Сохранить:

```bash
KEY=gmq_...                       # plaintext — в секреты
MATERIAL='gmq_xxxxxxxx:$argon2id$...'   # целиком, одним куском
```

## 2. Поднять api с открытым окном активации

```bash
GRAMMQ_DATABASE_URL=postgresql+asyncpg://gram:gram@localhost:5432/gram_mq \
GRAMMQ_KEY_BOOTSTRAP_ENABLED=true \
GRAMMQ_KEY_BOOTSTRAP_MATERIAL="$MATERIAL" \
uv run gram-mq-api &              # 127.0.0.1:8080
```

Ожидание: процесс стартует; в логах нет материала (только штатный запуск).

## 3. Активировать ключ

```bash
curl -s -i -X POST http://127.0.0.1:8080/bootstrap/api-key
```

Ожидания:
- `201` + `{"status":"activated","prefix":"gmq_xxxxxxxx"}`; в теле нет
  хэша и plaintext;
- повторный такой же POST → `409 {"detail":"api key already activated"}`
  (идемпотентность, US3);
- в БД — ровно одна новая строка:

```bash
docker exec postgres psql -U gram -d gram_mq -t \
  -c "SELECT prefix, left(key_hash,20)||'...', revoked_at FROM gmq_api_keys;"
```

## 4. Ключ работает (немедленно, FR-011)

```bash
curl -s -i -X POST http://127.0.0.1:8080/api/v1/messages \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"bot_slug":"any","chat_id":"123","text":"bootstrap-check"}'
```

Ожидание: `404 bot not found` (бот не зарегистрирован) — это успех
авторизации: ключ прошёл действующую проверку `X-API-Key` (без ключа было
бы `401`). С зарегистрированным ботом (quickstart 003) — `202 {"id":...}`.

## 5. Закрыть окно — ключ продолжает жить

```bash
kill %1   # и перезапустить api УЖЕ без GRAMMQ_KEY_BOOTSTRAP_ENABLED
GRAMMQ_DATABASE_URL=... uv run gram-mq-api &
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8080/bootstrap/api-key
# → 404: маршрут «исчез» (SC-002)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8080/api/v1/messages \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"bot_slug":"any","chat_id":"123","text":"still-alive"}'
# → 404 (или 202 с ботом): ключ авторизуется — он в БД, не в настройке (SC-004)
```

## 6. Отрицательные проверки

- Шаг 3 на api, стартовавшем без флага → `404` (FR-007).
- `GRAMMQ_KEY_BOOTSTRAP_ENABLED=true` без материала → `503` (FR-012);
  материал с битым prefix/хэшем (`gmq_bad:xxx`) → тоже `503`, строк в БД
  не прибавилось.
- `grep -r "$MATERIAL" <логи api>` → пусто (FR-015, SC-003).

## Амвера-вариант (без локального curl к серверу)

Шаги 1–2 переносятся один в один: материал и флаг задаются переменными
окружения в панели, применяются редеплоем; «пнуть» — `curl -X POST
https://<домен>/bootstrap/api-key` извне; шаг 5 — убрать флаг в панели и
сделать редеплой. Проверки ответов те же.
