# Gram-MQ Constitution

<!--
Sync Impact Report (temporary note for review — remove before committing)
- Version change: 2.2.0 → 2.3.0 (MINOR: a new Configuration standard
  added; "Stack and deployment (v1)" materially expanded)
- Modified principles: none (Core Principles I–IV unchanged)
- Modified standards: "Stack and deployment (v1)" — PostgreSQL 16 may now
  be co-located with the services or on a dedicated instance reachable
  over TCP (sslmode=require preferred); a cross-reference to the new
  Configuration standard added (pydantic-settings is no longer only a
  stack mention)
- Added sections: Engineering Standards > "Configuration" — GRAMMQ_-prefixed
  BaseSettings subclasses are the only settings channel; non-secret
  defaults are versioned in code; secret-bearing fields are required,
  typed SecretStr, and fail fast; per-host overrides and secrets live in
  a single env-file outside the deploy tree, located via GRAMMQ_ENV_FILE
  (one systemd pointer line in production, ./.env fallback locally),
  never committed, never deployed; api and worker share the file;
  TCP-with-password is the DB default, Unix-socket peer auth is an
  allowed co-located optimization; real env vars take precedence over the
  env-file; bot tokens stay in bots/.env-<bot_slug> per Principle III
- Removed sections: none
- Follow-up TODOs: none
-->

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

- There are no secrets in the DB — only hashes: API keys and passwords —
  Argon2id; OTP and refresh tokens — HMAC-SHA256.
- The bot token is stored only in `bots/.env-<bot_slug>` on disk; never
  in SQL, API responses, or the dashboard. No file — no messages are sent
  by that bot.
- A plaintext API key never crosses HTTP: the key is issued only by a CLI
  on the server and printed to the terminal once. API and dashboard
  endpoints return only `prefix` + `key_hash`. Revocation — `revoked_at`;
  rotation — a new key plus revoking the old one.
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
  mechanism); there is no HTTP self-registration. Registering bots over
  HTTP is forbidden — CLI on the server only.

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
- DB tests run only when `TEST_DATABASE_URL` is set; otherwise they are
  skipped.
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
- Bot tokens live in `bots/.env-<bot_slug>` files under Principle III and
  do not pass through the settings classes.

Rationale: versioned defaults (code) plus per-host secrets (one file
outside the deploy tree) leave no secret reachable through git, deploy
scripts, or backups, while every entry point — api, worker, CLI, Alembic,
tests — reads configuration the same way.

### Stack and deployment (v1)

- Stack: Python 3.12+, uv, FastAPI + Pydantic v2 + pydantic-settings,
  SQLAlchemy 2 (async) + asyncpg + Alembic, PostgreSQL 16 — co-located
  with the services or on a dedicated instance reachable over TCP
  (`sslmode=require` where possible), aiogram 3 (`Bot` only), React +
  TypeScript + Vite + MUI (MIT).
- Development tools: ruff, mypy --strict, pytest + pytest-asyncio,
  coverage.
- Deployment: systemd (api + worker as separate units), uv venv on the
  host, no Docker or Compose.
- The queue is the journal: `PostgresBroker` works on the `messages`
  table; there is no separate broker in v1.
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
- The constitution and all Spec Kit artifacts (specs, plans, tasks,
  checklists) are written in English.
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

**Version**: 2.3.0 | **Ratified**: 2026-09-23 | **Last Amended**: 2026-10-04
