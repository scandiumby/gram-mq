# Quickstart: проверка фичи 001 end-to-end

Руководство по ручной/CI-валидации. Детали схемы — [data-model.md](data-model.md),
сигнатуры — [contracts/broker-port.md](contracts/broker-port.md); реализация
и полный тест-код — в tasks.md / фазе имплементации.

## Предусловия

- Python 3.12+, uv.
- `uv sync --group dev` (устанавливает SQLAlchemy/asyncpg/Alembic + pytest).
- Демо настроек: скопируйте `.env.example` в `.env` и заполните
  `GRAMMQ_DATABASE_URL` (миграции в п.2 читают её через настройки).
- Для Postgres-прогона: доступная PostgreSQL 17 и настроенный тестовый URL —
  либо переменная окружения `GRAMMQ_TEST_DATABASE_URL` (алиас для CI:
  `TEST_DATABASE_URL`), либо `./.env.test` (скопируйте `.env.test.example`
  и заполните `GRAMMQ_TEST_DATABASE_URL`, например
  `postgresql+asyncpg://gram:gram@localhost:5432/gram_mq_test`); без него
  Postgres-параметризация молча пропускается.

## 1. Контрактные тесты и статические гейты (in-memory, без инфраструктуры)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy .                                   # strict, 0 ошибок
uv run coverage run --omit="src/gram_mq/adapters/postgres/*" -m pytest tests/contract -q
uv run coverage report --fail-under=80          # порог Testing-стандарта
```

Ожидание: линт и типы чисты; все сценарии историй 1–5 спеки проходят
против InMemoryBroker; каждый публичный метод порта покрыт тестом;
coverage ≥ 80%. Прогон занимает секунды и не требует БД. Без
TEST_DATABASE_URL DB-gated адаптер исключается флагом `--omit`; полный
coverage-прогон с БД (п.2) идёт без `--omit` и покрывает его целиком.

## 2. Контрактные тесты (PostgresBroker)

```bash
# Схему в тестовую базу сюит накатывает сам (alembic upgrade head перед
# прогоном). Тестовая БД — GRAMMQ_TEST_DATABASE_URL / .env.test
# (CI-алиас TEST_DATABASE_URL тоже работает).
TEST_DATABASE_URL=... uv run coverage run -m pytest tests/contract -q
uv run coverage report --fail-under=80
```

Ожидание: тот же сюит проходит с тем же результатом (FR-010, SC-002).
Без настроенной тестовой БД Postgres-часть молча пропускается (skipif) —
конституция, Engineering Standards → Testing.

## 3. Ручной smoke: полный цикл на in-memory брокере

```bash
uv run python - <<'PY'
import asyncio
from gram_mq.adapters.memory.broker import InMemoryBroker
from gram_mq.ports.broker import OutboundMessage
from uuid import uuid4

async def main():
    br = InMemoryBroker()
    msg = OutboundMessage(id=uuid4(), bot_slug="1234-arrakis-stillsuit",
                          chat_id="42", payload={"text": "hello"})
    await br.enqueue(msg)                      # 1. постановка
    assert await br.queue_depth("1234-arrakis-stillsuit") == 1
    d = await br.claim(worker_id="w1")         # 2. захват с лизом
    assert d.id == msg.id
    assert await br.claim(worker_id="w2") is None   # живой лиз не отдаётся
    d.context["telegram_message_id"] = "777"
    await br.ack(d)                            # 3. атомарная фиксация
    assert await br.queue_depth("1234-arrakis-stillsuit") == 0
    print("smoke OK")

asyncio.run(main())
PY
```

Ожидание: `smoke OK` — постановка, учёт глубины, эксклюзивность лиза,
атомарный ack работают без какой-либо инфраструктуры.

## 4. Сценарий «падение воркера» (ключевой, SC-001)

Покрыт контрактным тестом: строка leased + истёкший
`lease_expires_at` (в in-memory — переводом инжектируемых часов, в
Postgres — `broker_lease_seconds=0.2` и пауза) снова отдаётся claim-ом
другому воркеру. Ручная проверка — тем же smoke-скриптом, добавив
«истечение» перед вторым claim.

## 5. Справедливость (SC-003)

Контрактный тест: 1000+ queued у бота A и по одному у B и C → в первых
10 захватах встречаются минимум два разных бота.

## Критерий готовности фичи

Все пункты 1–2 зелёные в CI (Postgres-часть — при наличии сервисной БД),
статические гейты (`ruff`, `mypy --strict`) и coverage ≥ 80% проходят,
smoke из пункта 3 воспроизводим локально после чистого клона.
