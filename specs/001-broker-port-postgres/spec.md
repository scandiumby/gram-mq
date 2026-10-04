# Feature Specification: Outbound Message Queue Behind a Broker Port

**Feature Branch**: `001-broker-port-postgres`

**Created**: 2026-09-26

**Status**: Draft

**Input**: User description: "Outgoing message queue behind a broker port: the domain contract BrokerPort (enqueue, claim, ack, retry, dead_letter, queue_depth), PostgresBroker on the messages table, an in-memory adapter for tests, contract tests against both adapters, Alembic migrations."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Enqueueing a message (Priority: P1)

As the API service (producer), I put an outgoing message into the target
bot's queue so that it is guaranteed to survive until delivery — even if
delivery happens later and by another process. The queue and the message
journal are a single store: an enqueued message stays visible forever
instead of disappearing after being sent.

**Why this priority**: without enqueueing there is no queue; this is the
minimal self-contained slice where the value of the whole system begins.

**Independent Test**: verifiable in isolation — enqueue a message and
confirm it gets the queued status, is counted in the bot's queue depth,
and stays in place after the process restarts.

**Acceptance Scenarios**:

1. **Given** a bot with an empty queue, **When** a message is enqueued,
   **Then** it has the queued status and the bot's queue depth is 1.
2. **Given** a message is enqueued, **When** the process working with the
   queue restarts, **Then** the message is in place and available for
   claiming (journal = queue).

---

### User Story 2 - Claiming a message into exclusive ownership (Priority: P1)

As the delivery worker, I take a message from the queue into temporary
exclusive ownership (a lease): while the lease is alive, no other worker
gets that row. Claiming distributes work between bots fairly: a bot with
a huge queue does not starve the others — every bot with work receives
claims.

**Why this priority**: claiming with a lease is the mechanism that makes
the queue safe for concurrent workers; fairness protects small bots from
starvation.

**Independent Test**: two bots with messages → a series of claims returns
messages from both bots instead of draining one bot's whole queue first;
a message in another worker's live lease is never handed out again.

**Acceptance Scenarios**:

1. **Given** queued messages on two bots, **When** a worker makes a series
   of claims, **Then** the first claims include messages from both bots
   (no single bot monopolizes the stream).
2. **Given** a message is claimed by worker A and its lease is alive,
   **When** worker B claims, **Then** that row is not handed to B.
3. **Given** the queue is empty, **When** a worker claims, **Then** the
   answer is "no work".

---

### User Story 3 - Surviving a worker crash (Priority: P1)

As the system, I survive the crash of any worker without losing messages:
if a worker died before finishing processing, the expired lease returns
the row to claimable state automatically — without manual intervention.
Redelivery is acceptable: the contract is at-least-once; a duplicate in
the chat is better than a loss.

**Why this priority**: this is a constitutional invariant of the project
(NON-NEGOTIABLE); reliability is not delegated to the process supervisor.

**Independent Test**: claim a message, "kill" its owner, let the lease
expire — the next claim returns the same row.

**Acceptance Scenarios**:

1. **Given** a message in leased with an expired lease, **When** any
   worker claims, **Then** the row is handed out for processing again.
2. **Given** a message is claimed and its lease is alive, **When** the
   lease has not expired, **Then** the row is handed to nobody but the
   owner (ownership holds).

---

### User Story 4 - Recording the processing outcome (Priority: P1)

As the worker, I record the processing outcome: success — the delivery
acknowledgement (the Telegram message id and the sent status) is recorded
atomically, in a single operation; a temporary failure (rate limit,
network) — the message goes back to the queue with a delay and an attempt
counter; exhausted attempts or a fatal error — the message is marked
failed with a reason and never claimed again, remaining in the journal.

**Why this priority**: without recording the outcome the queue can neither
finish a delivery, nor retry safely, nor stop infinite retries.

**Independent Test**: the three branches are verified in isolation — ack,
retry (not claimable until the delay passes), dead_letter after the
attempt limit.

**Acceptance Scenarios**:

1. **Given** a message is claimed, **When** the worker confirms success,
   **Then** the sent status and the Telegram message id are recorded in
   one operation.
2. **Given** a message is retried with a delay, **When** the delay has not
   passed yet, **Then** claiming does not return it; **When** the time
   comes, **Then** the row is available and the attempt counter is
   incremented.
3. **Given** the attempt limit is exhausted, **When** the worker records
   a fatal outcome, **Then** the status is failed with a reason; claiming
   never returns the row again; the row stays in the journal.

---

### User Story 5 - Queue depth for the operator (Priority: P2)

As the operator, I see the queue depth for every bot — the number of
undelivered messages (waiting + in processing; delivered and failed are
not counted) — to notice jams and monitor the cleanup after incidents.

**Why this priority**: observability is needed on top of a working queue;
without stories 1–4 there is nothing to show.

**Independent Test**: a bot without undelivered messages has depth 0;
after an enqueue it grows, after a delivery acknowledgement it drops.

**Acceptance Scenarios**:

1. **Given** a bot has no undelivered messages, **When** the operator
   requests the depth, **Then** the answer is 0.
2. **Given** a message is enqueued and claimed, **When** the depth is
   requested, **Then** it is counted (a claimed message is undelivered).

---

### User Story 6 - A lightweight queue double for tests (Priority: P2)

As a developer, I test domain logic (API, worker) against a lightweight
in-memory queue double with the same semantics of statuses, leases, and
attempts, without deploying infrastructure. Semantic unity is confirmed
by a single contract test suite that runs against both the double and the
real queue implementation (when a test DB is configured).

**Why this priority**: it speeds up and stabilizes the tests of everything
above the queue; it requires the working contract from stories 1–4.

**Independent Test**: the contract scenario suite produces the same result
against both implementations.

**Acceptance Scenarios**:

1. **Given** the single contract test suite, **When** it runs against the
   in-memory double, **Then** all scenarios of stories 1–5 pass.
2. **Given** a test DB is configured, **When** the same suite runs against
   the primary implementation, **Then** the results are identical.

---

### Edge Cases

- Two workers try to claim the same row simultaneously — exactly one gets
  it; the other gets different work or "no work".
- A worker disappears forever without finishing — the expired lease
  returns the row to work; the message is not lost, redelivery is
  possible (the at-least-once contract).
- A message is retried before its delay has passed — claiming does not
  return it (backoff is enforced by the queue, not by worker discipline).
- The attempt limit is exhausted — the message is failed forever with a
  reason; infinite retry loops are impossible.
- Enqueueing a message for an unregistered bot is impossible (referential
  integrity onto the bot).
- The queue depth of a bot with no messages is 0, not an error.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a domain queue contract with the
  operations enqueue, claim, ack, retry, dead_letter, queue_depth; the
  semantics are domain-level, not protocol-specific (not AMQP).
- **FR-002**: enqueue MUST record a message with the queued status; the
  queue and the journal are a single store; an enqueued message does not
  disappear after delivery.
- **FR-003**: claim MUST hand out only rows with the queued status whose
  available_at has arrived, moving them to leased while recording the
  owner (locked_by), the claim moment (locked_at), and the lease deadline
  (lease_expires_at).
- **FR-004**: claim MUST distribute work fairly between bots: one bot's
  large queue must not starve the other bots' messages; the specific
  fairness mechanism is the plan's decision.
- **FR-005**: A row in leased with an expired lease_expires_at MUST
  automatically become claimable without manual intervention.
- **FR-006**: ack MUST record the delivered message id
  (telegram_message_id) and the sent status in one atomic operation.
- **FR-007**: retry MUST return the row to queued with
  available_at = now + delay, attempts incremented by 1, and ownership
  released.
- **FR-008**: After max_attempts is exhausted, dead_letter MUST move the
  row to failed with a reason text; claim never returns it again; the row
  stays in the journal.
- **FR-009**: queue_depth MUST return the number of the bot's undelivered
  messages (statuses queued and leased); sent and failed are not counted.
- **FR-010**: The contract MUST have a second implementation — an
  in-memory double — with identical semantics of statuses, ownership, and
  attempts; a single contract test suite runs against both
  implementations (the double always, the primary one when a test DB is
  configured).
- **FR-011**: The claim result (Delivery) MUST be opaque to the domain:
  its internals belong to the implementation; the domain never inspects
  it.
- **FR-012**: The data schema (the messages table and the bots table) MUST
  be applied by versioned migrations; the bots table contains only the
  bot_slug identifier and a creation timestamp — no tokens or settings.
- **FR-013**: New contract methods MUST appear only when a concrete queue
  implementation needs them (the contract grows on demand, not in
  advance).

### Key Entities *(include if feature involves data)*

- **Outbound message (OutboundMessage)**: id (UUID), bot_slug, chat_id,
  payload (text and formatting); many messages to one bot.
- **Delivery record (journal row)**: status (queued / leased / sent /
  failed), available_at, locked_at, locked_by, lease_expires_at, attempts,
  max_attempts, telegram_message_id, error, created_at, sent_at.
- **Delivery**: an opaque handle of the claimed row, issued by the queue
  to the worker.
- **Bot**: bot_slug — the single bot identifier; no tokens or settings in
  this entity.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A message accepted into the queue survives an abnormal
  termination and a restart of the delivery process without manual
  intervention — confirmed by the "worker crash" scenario test against
  both implementations.
- **SC-002**: The full contract acceptance suite (stories 1–5) produces an
  identical result against both queue implementations.
- **SC-003**: With one bot's queue at 1000+ messages and other bots having
  messages, the first 10 claims contain messages from at least two
  different bots — starvation is ruled out.
- **SC-004**: Message enqueue time does not depend on the depth of the
  already accumulated queue (enqueueing does not degrade as the journal
  grows).

## Assumptions

- The concrete values of the attempt limit (max_attempts), the lease
  duration, and the retry delays are set by the implementation plan; the
  spec fixes only the semantics.
- The fairness mechanism of claiming (round-robin, weighted, random) is
  the plan's decision; the spec requires only the absence of starvation.
- Delivery duplicates are acceptable and documented: the delivery contract
  is at-least-once (Constitution, Principle II).
- The first implementation of the port is PostgresBroker on the messages
  table; the in-memory double is for tests (Constitution, Principle I).
  A RabbitMQ adapter is outside this feature.
- Tests of the primary implementation require a configured test DB
  (TEST_DATABASE_URL); without it they are skipped (Constitution,
  Engineering Standards, "Testing").
- The DB schema is applied by Alembic migrations (Constitution,
  Engineering Standards, "Database migrations").
