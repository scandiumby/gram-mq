# Contract: BrokerPort

Доменный контракт очереди исходящих сообщений. Расположение:
`src/gram_mq/ports/broker.py`. Реализации: `adapters/postgres/broker.py`
(PostgresBroker), `adapters/memory/broker.py` (InMemoryBroker, тестовый
двойник). Контракт доменный, не AMQP; новые методы добавляются только при
потребности конкретного адаптера (FR-013, конституция принцип I).

## Типы

```python
class OutboundMessage:          # вход enqueue
    id: UUID                    # генерируется постановщиком
    bot_slug: str
    chat_id: str
    payload: dict               # v1: {"text": str, "parse_mode": str | None}

class Delivery:                 # результат claim, непрозрачен для домена
    id: UUID                    # = OutboundMessage.id (инвариант адаптера)
    bot_slug: str
    chat_id: str
    payload: dict
    context: dict               # {"attempts": int} — только для чтения воркером
```

`Delivery` — handle адаптера: домен не разбирает его устройство и не
конструирует сам; внутренние поля — деталь реализации.

## Операции

```python
class BrokerPort(Protocol):
    async def enqueue(self, message: OutboundMessage) -> None:
        """Поставить сообщение: строка status=queued, available_at=now().
        Бросает исключение, если bot_slug не зарегистрирован (FK)."""

    async def claim(self, *, worker_id: str) -> Delivery | None:
        """Захватить доступную работу в исключительное владение:
        queued с наступившим available_at или leased с истёкшим лизом
        → leased с новым лизом. Справедливость между ботами (FR-004,
        механизм RQ-4). Работы нет — None."""

    async def ack(self, delivery: Delivery) -> None:
        """Зафиксировать доставку: status=sent + telegram_message_id
        (delivery.context["telegram_message_id"]) + sent_at одной
        транзакцией. Не-leased строка/чужой владелец — ValueError."""

    async def retry(self, delivery: Delivery, delay: timedelta, reason: str) -> None:
        """Вернуть в очередь: status=queued, available_at=now()+delay,
        attempts+=1, лиз сброшен, error=reason."""

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        """Похоронить: status=failed, error=reason, лиз сброшен.
        Терминально: claim больше не отдаёт."""

    async def queue_depth(self, bot_slug: str) -> int:
        """Число недоставленных сообщений бота (queued + leased)."""
```

## Инварианты (проверяются контрактными тестами обоих адаптеров)

1. enqueue → строка queued, учитывается в queue_depth (истории 1, 5).
2. Одновременный claim одной строки — ровно один победитель (SKIP LOCKED
   в Postgres; блокировка объекта в in-memory).
3. Строка в живом лизе недоступна чужому claim; с истёкшим — доступна
   любому (at-least-once, FR-005).
4. retry до наступления available_at не отдаётся claim-ом (backoff
   принудителен).
5. sent и failed терминальны для claim, но вечны в журнале.
6. Семантика идентична у обоих адаптеров (единый тест-сюит).

## Границы контракта (что НЕ входит)

- Метод `ensure_bot_queue` отсутствует: в Postgres «очередь бота» = строки
  с bot_slug; метод появится, когда его потребует адаптер RabbitMQ.
- Политика backoff и решение «retry или dead_letter» — вызывающий (воркер).
- Продление лиза (`renew`) — не входит в v1: инвариант — длительность лиза
  (60 с по умолчанию, RQ-1) превышает худшее время отправки; затянувшаяся
  или зависшая отправка разрешается истечением лиза и реклеймом —
  возможен дубль, допустимый контрактом at-least-once.
- Пакетный claim, приоритеты, отложенная отправка по расписанию — вне
  контракта v1.
