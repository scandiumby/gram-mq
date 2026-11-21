# Gram-MQ Constitution

## Core Principles

### I. Ports and adapters, not specific technologies

The domain (API, worker, dashboard) depends only on ports: `BrokerPort`,
`TelegramSender`, `AuthProvider`, `BotConfig`. Which implementation sits
behind them — `PostgresBroker`, `RabbitBroker`, in-memory, aiogram, fake —
is unknown to the domain.

- `BrokerPort` is a domain contract (`enqueue` / `claim` / `ack` /
  `retry` / `dead_letter` / `queue_depth`), not AMQP. New port methods
  appear only when a concrete adapter needs them (example:
  `ensure_bot_queue` for RabbitMQ).
- `Delivery` is an opaque adapter handle; the domain never inspects it.
- A new adapter is added only through a Spec Kit specification
  (spec → plan → tasks), never "along the way".

Rationale: switching the broker, the login provider, or the Telegram
client is a new adapter, not a rewrite of FastAPI, the worker, and the
dashboard.

### II. No message is lost — at-least-once delivery (NON-NEGOTIABLE)

Every accepted message must survive the crash of any process.

- The lease (`lease_expires_at`) and the `attempts` counter are mandatory;
  "relying on systemd" is forbidden. A worker crash loses no rows: they
  stay `queued` or become claimable again after the lease expires.
- Ack is atomic: `telegram_message_id` and `status=sent` are written in a
  single transaction immediately after a successful Telegram response.
- Delivery is at-least-once, not exactly-once: a duplicate in the chat
  after a break between send and commit is acceptable and documented.
  This is a contract property, not a schema defect.
- Retry (429, network) — `available_at = now() + backoff`,
  `attempts += 1`; after `max_attempts` — `failed` (a DLQ analogue).
- API and worker are separate processes: an HTTP request never waits for
  Telegram. Telegram rate limits live in the worker, not in the broker.

### III. No plaintext secrets in the system

- There are no plaintext secrets in the DB: API keys and passwords are
  stored as Argon2id hashes, OTP and refresh tokens as HMAC-SHA256. The
  single exception is the encrypted table half of a bot token (next
  bullet), which without the master key is not a secret.
- The bot token is stored split into two incomplete halves, each encrypted
  with AESGCM under a dedicated token master key: the file half (numeric
  id + ":" + the first 4 characters of the secret part) in
  `bots/.env-<bot_slug>`, the table half (the rest of the secret part) in
  the bots table. Both halves of a pair share a random pairing nonce used
  as AEAD associated data, so mixing halves of different bots or
  generations fails decryption locally, before any Telegram call. Neither
  store alone yields a usable token. The plaintext token exists only in
  the bootstrap setting during a creation/rotation window and briefly in
  process memory — never in SQL in the clear, in API responses, in logs,
  or on the dashboard. No decryptable pair — no messages are sent by that
  bot.
- A plaintext API key never crosses HTTP: keys are issued only by a CLI —
  `gram-mq key add` where server shell access exists, or `gram-mq key
  prepare` run locally, with server registration through an activation
  endpoint that is disabled by default and accepts only the pre-computed
  hash (shell-less deploys). Either way the plaintext is printed to the
  terminal exactly once and never leaves the operator's machine; the
  server receives only prefix + hash. API and dashboard endpoints return
  only `prefix` + `key_hash`. Revocation — `revoked_at`; rotation — a new
  key plus revoking the old one.
- The access token lives in an httpOnly cookie, not in localStorage. OTP
  is never logged.
- The `bots/` directory is never committed (`.gitignore`).

### IV. Two authorization realms, separate URL spaces

- Humans: `AuthProvider` (v1 — `TelegramOtpProvider`) → JWT in an
  httpOnly cookie → `/api/dashboard/*`. Machines: the `X-API-Key` header
  on all of `/api/v1/*` (except health). Machines never use dashboard
  URLs; humans never use `/api/v1/*` with a key.
- Changing the login method (password, phone, SMS) = a new
  `AuthProvider` adapter plus a column/form, not a new "login server".
  JWT issuance, cookies, refresh, and API keys do not depend on the
  provider.
- The first user is created by an administrator (a CLI/bootstrap
  mechanism); there is no HTTP self-registration. Bot registration runs
  through a bootstrap endpoint that is disabled by default and takes the
  full token from a secret setting — the request body carries no secrets —
  or through the CLI wizard; both paths write the same encrypted halves.
  The endpoint is enabled only for a creation/rotation window, exactly
  like the API key activation endpoint.
- Telegram webhooks are a third, permanent URL space:
  `POST /webhooks/{bot_slug}`, outside both `/api/v1/*` and
  `/api/dashboard/*`. The caller is Telegram itself, so neither an API
  key nor a session applies: requests authenticate by the per-bot
  webhook secret in the `X-Telegram-Bot-Api-Secret-Token` header,
  verified constant-time against an HMAC-SHA256 check value (Principle
  III) stored on the bot's registry row; the plaintext secret exists
  only inside the registering CLI for the duration of one `set_webhook`
  call. The surface is wired only when the webhook master key is
  configured; a bot without a check value — disabled reception or
  revoked — answers the same 404 as an unknown bot.

## Engineering Standards

Mandatory norms of engineering discipline. They carry the same force as
the Core Principles; the difference is the subject: principles describe
system properties, standards describe how code is written (see
Governance).

### Asynchrony

There are no blocking calls in runtime code (the api and worker
processes).

- Forbidden: `requests`, `time.sleep`, synchronous `psycopg2`, and any
  other synchronous HTTP/DB clients in the event loop. Use async clients
  instead (httpx, SQLAlchemy async + asyncpg) and `asyncio.sleep`.
- A library without an async API is either replaced or called through
  `asyncio.to_thread` with a justification comment at the call site.
- CLI utilities and one-off administrative scripts are not runtime code.

Rationale: a blocking call stops the event loop of the whole process —
the API stops answering health checks, the worker stops renewing leases;
for at-least-once delivery this is a direct threat.

### Type checking

- `mypy --strict` passes over the entire project codebase without
  errors — a mandatory pre-merge gate.
- `# type: ignore` is allowed only with an adjacent justification (a
  comment or an issue link); "bare" ignores are not allowed.

Rationale: strict typing is the cheapest static test; an ignore without
a reason hides a defect instead of fixing it.

### Database migrations

- The Postgres schema changes only through Alembic revisions.
  `CREATE TABLE`, `ALTER`, `DROP`, and `Base.metadata.create_all()` are
  forbidden in application code.
- The Alembic history is the single source of truth for the schema;
  `alembic upgrade head` runs before services start.

Rationale: a schema changed outside migrations cannot be reproduced on a
clean environment and silently diverges between dev and prod.

### Testing

- Every public method and function is covered by at least one test.
- Test coverage never drops below 80%.
- Unit tests use the in-memory broker and a fake Telegram sender; real
  secrets and `.env` are never used in tests.
- DB tests run only when a test database is configured —
  `GRAMMQ_TEST_DATABASE_URL` (CI alias `TEST_DATABASE_URL`) or
  `./.env.test` via `GRAMMQ_TEST_ENV_FILE`; otherwise they are skipped.
  Tests never read the deploy env-file.
- Fast tests must cover port behavior, key/OTP hashing, and slug
  generation.

Rationale: an untested public method is undocumented behavior; the 80%
threshold keeps coverage from degrading unnoticed.

### Dependency licenses

- All project dependencies (runtime and dev) are under licenses
  compatible with MIT: MIT, BSD-2/3-Clause, ISC, Apache-2.0, PSF, and
  similar permissive ones. Copyleft (GPL, AGPL) is not allowed.
- Before adding a dependency, its license is checked; a dependency
  without a clear license is not added.

Rationale: a copyleft or unlicensed dependency restricts distribution
and embedding of the project; permissive MIT-compatible licenses carry
no such risk.

### Configuration

All runtime configuration is loaded exclusively through pydantic-settings
classes.

- Settings live in `BaseSettings` subclasses with the shared `GRAMMQ_`
  environment prefix. Direct `os.environ` / `os.getenv` reads in runtime
  code are forbidden; one-off CLI scripts are not runtime code (the same
  boundary as in Asynchrony).
- Defaults for non-secret values live in the settings classes and are
  versioned in code. A default is a value that is true in every
  environment; values that differ between hosts or environments are
  passed through the environment, not by editing a default.
- Secret-bearing fields (first of all `GRAMMQ_DATABASE_URL` carrying a
  password for a remote DB) are required, have no defaults, and are typed
  as `SecretStr`: `repr()` of the settings must not reveal a secret in
  logs or error messages. A missing value aborts the process at startup
  (fail-fast).
- Per-host overrides and secrets live in a single env-file outside the
  deploy tree (e.g. `/etc/gram-mq/.env`). Its path comes from the
  `GRAMMQ_ENV_FILE` variable; in production this is the single
  `Environment=` line of the systemd unit (a pointer, no values),
  locally the fallback is `./.env`. The file is never committed and
  never deployed; deploy and backup procedures neither touch it nor
  include it in archives. In production the file usually contains at
  least `GRAMMQ_DATABASE_URL`.
- api and worker read the same env-file (different fields); separate
  `api.env` / `worker.env` files are not created, and configuration
  values are not written into systemd units.
- DB connectivity does not assume locality. The default is a TCP
  connection with the password inside `GRAMMQ_DATABASE_URL` (a dedicated
  instance; `sslmode=require` where possible). When the DB is co-located,
  a Unix-socket connection with local peer auth (`pg_hba.conf`, a
  dedicated user) and a passwordless URL is allowed — an optimization of
  a specific host, not the norm.
- Source priority is the pydantic-settings default: real environment
  variables take precedence over the env-file.
- Bot tokens do not pass through the settings classes, with one windowed
  exception: a secret setting carries the full token of the bot being
  created or rotated — set for the operation, read by the bootstrap
  endpoint, cleared afterwards. Persistent token storage is governed by
  Principle III (encrypted halves in the file and the bots table); the
  token master key is a SecretStr setting with no default, used for
  nothing but those halves.

Rationale: versioned defaults (code) plus per-host secrets (one file
outside the deploy tree) leave no secret reachable through git, deploy
scripts, or backups, while every entry point — api, worker, CLI, Alembic,
tests — reads configuration the same way.

### Stack and deployment (v1)

- Stack: Python 3.12+, uv, FastAPI + Pydantic v2 + pydantic-settings,
  SQLAlchemy 2 (async) + asyncpg + Alembic, PostgreSQL 17 — co-located
  with the services or on a dedicated instance reachable over TCP
  (`sslmode=require` where possible), aiogram 3 (`Bot` only), React +
  TypeScript + Vite + MUI (MIT).
- Development tools: ruff, mypy --strict, pytest + pytest-asyncio,
  coverage.
- Deployment: systemd (api + worker as separate units), uv venv on the
  host, no Docker or Compose.
- The queue is the journal: `PostgresBroker` works on the single messages
  journal table; all DB object names are prefixed via `GRAMMQ_TABLE_PREFIX`
  (default `gmq`, e.g. `gmq_messages`), fixed when the first migration is
  applied; there is no separate broker in v1.
- `bot_slug` — a human-readable (slug-safe ASCII) single bot identifier
  across the inbound API, `messages`, and the config file name.
- Runtime configuration is governed by the Configuration standard (see
  Engineering Standards): pydantic-settings classes with defaults in code
  and a single env-file outside the deploy tree.

## Governance

- The constitution overrides individual feature material and local
  practices: in a conflict, it wins.
- The Principles and the Engineering Standards are equally binding:
  principles describe system properties, standards describe code
  discipline; the difference is the subject, not the force.
- Spec Kit feature artifacts (spec.md, plan.md, research.md, data-model.md,
  contracts/, quickstart.md, tasks.md, checklists) are written in Russian —
  the project's working language. Code, identifiers, commit messages, and
  the constitution itself are maintained in English.
- Spec Kit generating commands (specify, clarify, plan, checklist, tasks,
  implement) must read the constitution — principles and standards — as
  input constraints of their artifacts.
- Compliance gates: **plan** fills the Constitution Check and ends with
  ERROR on an unjustified violation; **analyze** marks a conflict between
  a requirement and a MUST-norm (principle or standard) as CRITICAL;
  **converge** spawns a task to fix the violated norm. Flow review gates
  after spec and plan are human checkpoints.
- Amendments are made only by an explicit constitution update
  ($speckit-constitution): semver — MAJOR when removing or redefining a
  principle or standard, MINOR when adding a principle or standard or
  materially expanding one, PATCH for wording refinements; every change
  updates the `Last Amended` date.
- The analyze and converge commands only read the constitution and never
  modify it.

**Version**: 3.1.0 | **Ratified**: 2026-09-23 | **Last Amended**: 2026-11-22
