# Gram-MQ Constitution

<!--
Sync Impact Report (temporary note for review — remove before committing)
- Version change: 2.1.0 → 2.2.0 (MINOR: five engineering MUST standards
  added. The whole amendment is still uncommitted, so the translation and
  the artifact-language rule fold into the same bump)
- Modified principles: none in substance (I–IV unchanged; the document is
  translated from Russian to English, titles re-worded in English)
- Added sections: "Engineering Standards" — Asynchrony, Type checking,
  Database migrations, Testing, Dependency licenses, Stack and
  deployment (v1)
- Added norms (Governance): the constitution and all Spec Kit artifacts
  (specs, plans, tasks, checklists) are written in English
- Removed sections: draft "Invariants" section, "Technical constraints
  (v1)" and "Test requirements" — merged without semantic change into
  "Engineering Standards" (the alembic upgrade head line moved to
  "Database migrations", test environment rules to "Testing")
- Minor edits: Governance gained the equal-force rule for principles and
  standards; gate wording changed from "MUST-principle" to "MUST-norm";
  the dev-tools line extended with mypy --strict and coverage
- Deferred TODOs: the repo LICENSE is GPL-3.0. The dependency-licenses
  standard (MIT-compatible dependencies) is compatible with this, but if
  the project is to be distributed under MIT, the LICENSE file must be
  replaced in a separate commit — outside the constitution's scope
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

### Stack and deployment (v1)

- Stack: Python 3.12+, uv, FastAPI + Pydantic v2 + pydantic-settings,
  SQLAlchemy 2 (async) + asyncpg + Alembic, PostgreSQL 16 on the host,
  aiogram 3 (`Bot` only), React + TypeScript + Vite + MUI (MIT).
- Development tools: ruff, mypy --strict, pytest + pytest-asyncio,
  coverage.
- Deployment: systemd (api + worker as separate units), uv venv on the
  host, no Docker or Compose.
- The queue is the journal: `PostgresBroker` works on the `messages`
  table; there is no separate broker in v1.
- `bot_slug` — a human-readable (slug-safe ASCII) single bot identifier
  across the inbound API, `messages`, and the config file name.

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

**Version**: 2.2.0 | **Ratified**: 2026-09-23 | **Last Amended**: 2026-09-27
