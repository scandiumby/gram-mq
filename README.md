# gram-mq
GramMQ — a smart Telegram proxy with message queue control. No message gets lost, even under heavy load.

## Synchronous Bot API proxy (feature 008)

The external (unstable) bot server never holds bot tokens: every Bot API
call that needs the result *now* goes through
`POST /api/v1/telegram/{bot_slug}/{method}` — the machine realm
(`X-API-Key`, same as `POST /api/v1/messages`). The endpoint is a pure
pipe: the token is reassembled in memory from the split store (feature
006), the JSON or multipart body travels to Telegram verbatim, and any
Telegram answer returns to the caller with its own status and body
(429 additionally echoes `Retry-After`; Telegram's 401 — a revoked bot
token — is remapped to 503 so it never collides with our own 401;
timeout/network → 504/502). No queues, no retries, no state: one
message — one path, delivery of new messages stays with the queue (the
anti-double consumer contract).

Method policy: `getUpdates`, `setWebhook`, `deleteWebhook`, `logOut`,
`close` are banned unconditionally (they fight the managed webhook/CLI
state); everything else is default-deny — the whitelist
`GRAMMQ_TELEGRAM_PROXY_ALLOWED_METHODS` (CSV) starts from the
immediate-reaction set (`getMe`, `answer*`, `edit*`/`stop*`/
`deleteMessage`, `sendChatAction`, `getChat`, `getChatMember`) and the
operator extends it without code changes; the banned list always wins.
Timeout and body cap (50 MB, Telegram's own upload ceiling) are
settings too. Logs carry bot+method+outcome+duration — never tokens,
keys, or bodies. Full contract for the external service:
`specs/008-telegram-api-proxy/contracts/telegram-api-proxy.md`.

## Inbound flow (feature 007)

Telegram updates of the registered bots arrive at
`POST /webhooks/{bot_slug}` — a third URL space (no `X-API-Key`, no
JWT) authenticated by the per-bot webhook secret Telegram sends in the
`X-Telegram-Bot-Api-Secret-Token` header. `gram-mq webhook set <slug>`
generates the secret, hands it to Telegram's `set_webhook`, and stores
only an HMAC-SHA256 check value (`gmq_bots.webhook_secret_hmac`, under
`GRAMMQ_WEBHOOK_MASTER_KEY`); `webhook delete` stops reception. The
handler does exactly one durable thing — the enqueue — and answers an
immediate 200 (Telegram treats slow answers as failed delivery and
resends); identical resends are absorbed by the soft dedup
(bot_slug + update_id + payload), while the same update_id with new
content is accepted as a new update.

The inbound queue is its own journal, `gmq_inbound_updates`
(queued / delivering / delivered / failed), with the same at-least-once
mechanics as the outbound one. A separate process,
`gram-mq-inbound-worker` (started by the single-container launcher when
`GRAMMQ_INBOUND_DELIVERY_URL` is set), delivers each payload verbatim
to the external bot server: timeouts/5xx/429 retry with exponential
backoff (cap 300 s, `max_attempts` → failed), other 4xx dead-letter
immediately. The receiver must answer 2xx only after processing and
dedup with a sliding window over (bot, update_id) — delivery is
at-least-once.

Operator queries (the queue is the journal):

```sql
-- status counts
SELECT status, count(*) FROM gmq_inbound_updates GROUP BY status;

-- queue depth (queued + delivering) and the age of the oldest pending
-- row: a growing age = the external server has been down longer than
-- you are willing to wait
SELECT count(*) AS depth,
       now() - min(created_at) AS oldest_age
FROM gmq_inbound_updates
WHERE status IN ('queued', 'delivering');

-- recent failures with their last error and attempt count
SELECT bot_slug, update_id, attempts, error, created_at
FROM gmq_inbound_updates
WHERE status = 'failed'
ORDER BY created_at DESC
LIMIT 20;

-- duplicate observations (absorbed Telegram retries leave no rows;
-- update_id reuse after a week of bot silence can): update_id seen
-- more than once per bot
SELECT bot_slug, update_id, count(*)
FROM gmq_inbound_updates
GROUP BY bot_slug, update_id
HAVING count(*) > 1;
```

Every lifecycle event (accept, absorbed duplicate, delivery retry,
outcome) is logged with the bot slug and `update_id` for correlation.

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
