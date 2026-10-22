# Harnessa architecture

## Product and service boundary

Harnessa is a self-hosted, single-organization modular monolith. `apps/api` owns HTTP, identity, authorization, lifecycle rules, persistence, and the API process. The separately runnable worker imports the same `apps/api/harnessa` domain, service, and adapter packages. PostgreSQL is the authority for tasks, run state, events, and dispatch eligibility. Redis Streams is a recoverable transport projection; losing Redis does not lose task state.

```text
apps/web ── HTTP + cookie auth ── apps/api ───── PostgreSQL 16
                                      │              │
                                      └── worker ── Redis Streams
packages/contracts ◀── generated from API OpenAPI
infra/                 Compose and digest-pinned images
```

The web app currently provides authentication, project selection, and repository configuration. The task and recovery surfaces are API-only in milestone 2. The worker uses `UnconfiguredExecutionAdapter` in production, so it publishes and consumes events, reconciles effect intents, and recovers leases without simulating agent activity or claiming tasks. No external model or GitHub connection is initiated.

## Package responsibilities and extension points

- `apps/api/harnessa/domain`: canonical task lifecycle graph and pure guard rules.
- `apps/api/harnessa/services`: authorized use cases, transition auditing, orchestration, retries, recovery, events, and effects. API and worker call these same services.
- `apps/api/harnessa/adapters`: explicit boundaries for execution, LiteLLM model routing, OpenSpec discovery, ACP sessions, AG-UI projection, Harness² probes, and artifact storage.
- `apps/api/harnessa/api`, `auth`, and `db`: HTTP routes, local identity, SQLAlchemy persistence, and Alembic migrations.
- `apps/web`: Next.js app shell and API consumers.
- `packages/contracts`: generated TypeScript view of the server's OpenAPI schemas; generated DTOs are not handwritten or duplicated.
- `infra`: Compose services and immutable image references.

Later milestones reserve specification revisions/questions, agent profiles and capabilities, provider/deployment/budget records, isolated workspaces, verification/review evidence, pull-request synchronization, Harness² evaluations, and artifact metadata/storage. No empty placeholder tables are created in milestone 2. Content-addressed local/object storage is an adapter contract only at this point.

## Task, run, and command invariants

`TaskStage` is the board lifecycle; `RunStatus` separately tracks queued, running, waiting, paused, failed, cancelled, and successful execution. The lifecycle graph is enforced in `domain/lifecycle.py` for user transitions, commands, scheduler transitions, and operator replay. Branches into clarification or Harness² probing store the interrupted return stage. User commands require an idempotency key, a reason, project authorization, and the observed task version (`If-Match`). A stale version returns HTTP 409 with `code=version_conflict` and `current_version`. Reusing an idempotency key for a different command returns HTTP 409.

Every state mutation writes a `TaskTransition` audit row and a versioned `DomainEvent`. The corresponding `OutboxMessage` is inserted in the same PostgreSQL transaction. Command replay returns the existing task result without repeating a mutation. System transitions use the same lifecycle guards and persisted audit/event shape; scheduler-owned stages cannot be forged through the user transition endpoint. Active pause/cancel commands update run status immediately, which prevents further dispatch, and call the execution adapter's cancellation hook for a current run.

The durable event envelope carries event and schema versions, organization/project/task/run scope, aggregate and project sequences, actor, occurrence time, correlation/causation IDs, and payload. Errors use the stable `code`, `message`, `request_id`, and `details` contract. Specification revision and artifact metadata schemas remain in the generated API contracts for later milestones.

## PostgreSQL and Redis orchestration

Alembic revision `ffac3c1be398` follows `0001_foundation`. It creates `task_transitions`, `runs`, `run_checkpoints`, `task_leases`, `domain_events`, `outbox_messages`, `consumer_inbox`, `effect_intents`, and `dead_letters`; it adds task priority, retry availability/cancellation fields, project concurrency limits, and the project event sequence. The earlier organization/user/membership/project/repository/task data remains intact when the new revision is applied. IDs are UUIDs, timestamps are timezone-aware, and project/task scoped keys have indexes and uniqueness constraints.

The API writes state and outbox rows atomically. The publisher leases outbox rows in short transactions and then calls Redis outside the database transaction. A crash can produce a duplicate `XADD`, so consumers use a unique `(consumer, event_id)` inbox insert before acknowledging. A crash after inbox commit but before `XACK` is safely redelivered and deduplicated. Redis is not dispatch authority: schedulers query PostgreSQL `READY` tasks, and `/ops/dispatch/rebuild` reconstructs the Redis dispatch projection from retained, still-queued task state.

Event replay is HTTP-based at `GET /projects/{project_id}/events?after=...&limit=...`. The monotonically increasing project cursor yields stable ordering, is authorized by project membership, and is bounded by `EVENT_REPLAY_LIMIT` (default 500). Event retention is bounded by `EVENT_RETENTION_DAYS` (default 30); cursors older than retained events receive HTTP 410. Redis Stream length is bounded by `REDIS_STREAM_MAXLEN` (default 10,000). There is no SSE/AG-UI stream subscription in this milestone.

The PostgreSQL scheduler uses a transaction-scoped advisory lock around claim accounting, row locks with `SKIP LOCKED`, and counts active leases against the global limit and each project's `max_concurrency`. It orders candidates by least recently dispatched project, then descending task priority, age, and ID. A claim atomically creates a run, increments the task's monotonic fencing token, establishes a renewable lease, advances the task to `IN_PROGRESS`, and appends the transition/event/outbox rows. The transaction commits before `ExecutionAdapter.execute` starts. Every heartbeat, checkpoint, completion, and failure checks owner, run ID, token, and lease expiry, so a stale worker cannot write over a replacement worker.

Checkpoints persist JSON state, a SHA-256 digest, sequence, run/task/project scope, resumability, and fencing token. If a lease expires, a retained resumable checkpoint permits a new fenced run to resume from that state. Without a checkpoint, recovery marks the run visibly failed and creates a dead letter instead of launching a possible duplicate. Retryable transient and rate-limited failures use capped exponential backoff with jitter (0.8–1.2 multiplier); explicit provider `Retry-After` is bounded to one hour. Attempts default to three and total wall time defaults to 1,800 seconds. Permanent failures and exhausted limits are dead-lettered. Operators can inspect project dead letters and replay a failed task with a reason and idempotency key.

Effect intents use a stable external-effect key and a reconciliation protocol that distinguishes `applied`, `not_applied`, and `unknown`. Adapter calls run outside the database transaction. An abandoned `reconciling` intent is reclaimed after 60 seconds; unknown outcomes use bounded retry delay. Until an effect provider adapter exists, the worker leaves intents retryable and performs no external write.

## HTTP, identity, and access

FastAPI Users provides local password hashing and cookie sessions. Administrator bootstrap is one-time and requires `BOOTSTRAP_TOKEN`; production startup requires a 32-character secret, a bootstrap token, and Secure cookies. Mutations require an exact allowlisted Origin and double-submit CSRF token. Organization and project roles are resolved from local memberships. The internal identity contract is designed for a later OIDC adapter; provider claims do not replace local membership authority.

Milestone 2 adds authorized task creation/list/detail, stage transition, pause/resume/cancel/retry commands, run and transition history, project event replay, dead-letter inspection/replay, and administrator dispatch rebuild endpoints. Each task, run, dead letter, and event endpoint resolves its owning project and checks membership before returning data. No artifact routes or artifact storage exist yet; those must use the same project authorization boundary.

Repository registration accepts only exact target IDs in `REPOSITORY_TARGETS`; it performs no GitHub connection, clone, fetch, or scan. LiteLLM, OpenSpec, ACP, AG-UI, Harness², external artifact storage, and GitHub integrations are extension contracts only.

## Local deployment and operational limits

`infra/compose.yaml` boots PostgreSQL, Redis, API, web, and worker. The API applies Alembic migrations before it serves requests; the worker waits for the healthy API and its schema. `.env.example` contains placeholders only. No model credential, GitHub credential, host engine socket, operator home directory, or SSH key is mounted into an agent. Milestone 2 includes no real execution adapter and makes no paid-service or live-model calls.
