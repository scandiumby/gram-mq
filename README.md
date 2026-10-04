# gram-mq
GramMQ — a smart Telegram proxy with message queue control. No message gets lost, even under heavy load.

## Queue module (feature 001)

The outbound message queue lives behind the domain port `BrokerPort`
(`enqueue` / `claim` / `ack` / `retry` / `dead_letter` / `queue_depth`) with
two interchangeable implementations: `PostgresBroker` (the queue is the
journal — one messages table, prefixed via `GRAMMQ_TABLE_PREFIX`, default
`gmq_messages`) and an in-memory double for fast tests.
Delivery is at-least-once: leases and attempt counters survive worker
crashes; a rare duplicate in the chat is the documented price of no loss.

### Run the contract tests

```bash
uv sync --group dev
uv run pytest tests/contract -q
```

The suite runs against the in-memory broker with no infrastructure.

### Run it against PostgreSQL

The optional test database URL is regular settings (Constitution,
Configuration — no raw env reads): copy `.env.test.example` to `./.env.test`
(tests never read the deploy `.env`) and fill in `GRAMMQ_TEST_DATABASE_URL`
once, and every later run picks it up automatically. A real env var —
`GRAMMQ_TEST_DATABASE_URL` or the CI alias `TEST_DATABASE_URL` — overrides
the file.

```bash
uv run alembic upgrade head      # applies the schema from GRAMMQ_DATABASE_URL
uv run coverage run -m pytest tests/contract -q && uv run coverage report --fail-under=80
```

With no URL configured the postgres parameterization is skipped, and
the local coverage gate omits the DB-gated adapter:

```bash
uv run coverage run --omit="src/gram_mq/adapters/postgres/*" -m pytest tests/contract -q
uv run coverage report --fail-under=80
```

Dev commands (defined in pyproject `[project.scripts]`): `uv run test`
(contract suite, full output), `uv run lint` (ruff check + format check),
`uv run typecheck` (mypy strict), `uv run check` (lint + typecheck in one
gate). Coverage gate (Constitution, Engineering Standards): ≥ 80%.
