# Harnessa architecture

## Product and service boundary

Harnessa is a self-hosted, single-organization modular monolith. `apps/api` owns HTTP, identity, authorization, lifecycle rules, persistence, and the API process. The separately runnable worker imports the same `apps/api/harnessa` domain, service, and adapter packages. PostgreSQL is the authority for tasks, run state, events, and dispatch eligibility. Redis Streams is a recoverable transport projection; losing Redis does not lose task state.

```text
apps/web ── HTTP + cookie auth ── apps/api ───── PostgreSQL 16
                                      │   │          │
                                      │   └── LiteLLM Proxy ── provider egress
                                      └── worker ── Redis Streams
packages/contracts ◀── generated from API OpenAPI
infra/                 Compose and digest-pinned images
```

The Next.js workspace provides the task board and inspector, OpenSpec explorer, Harness Studio, evaluation evidence, model routing, durable events, and GitHub integration views. Project/repository setup and authorization remain in the project configuration surfaces. The Compose worker publishes and consumes events, reconciles effect intents, and recovers leases. An optional host-side worker can construct the ACP adapter, validate rootless Podman, and start a run-scoped Unix-socket model broker. The broker is disabled by default and calls Harnessa's ModelRouter with the active task/run and fencing identity; the agent container has no provider network or provider key. LiteLLM is configured with the `harnessa-deepseek-flash`, `harnessa-openrouter-deepseek-flash`, and `harnessa-openrouter-gpt-oss-20b` aliases; provider keys are mapped only to the LiteLLM service. A configured alias does not imply account capability or a provider request; Harnessa still applies project/task/run budgets before routing.

## Package responsibilities and extension points

- `apps/api/harnessa/domain`: canonical task lifecycle graph and pure guard rules.
- `apps/api/harnessa/services`: authorized use cases, transition auditing, orchestration, retries, recovery, events, and effects. API and worker call these same services.
- `apps/api/harnessa/adapters`: explicit boundaries for execution, the typed LiteLLM router interface, OpenSpec discovery, bounded ACP sessions, the rootless Podman sandbox, managed workspaces, AG-UI projection, Harness² probes, and artifact storage.
- `apps/api/harnessa/api`, `auth`, and `db`: HTTP routes, local identity, SQLAlchemy persistence, and Alembic migrations.
- `apps/web`: Next.js app shell and API consumers.
- `packages/contracts`: generated TypeScript view of the server's OpenAPI schemas; generated DTOs are not handwritten or duplicated.
- `infra`: Compose services and immutable image references.

Milestone 3 adds provider accounts, LiteLLM deployments, capability-checked aliases, workload pools, project/task/run budget counters, expiring reservations, and an append-only redacted model request ledger. Milestone 5 adds per-run managed workspaces, a pinned ACP client, local content-addressed patch storage, and fenced `run_artifacts` metadata. It does not add a public artifact download route. Durable clarification questions remain reserved for milestone 6; the ACP clarification tool is not exposed.

## Task, run, and command invariants

`TaskStage` is the board lifecycle; `RunStatus` separately tracks queued, running, waiting, paused, failed, cancelled, and successful execution. The lifecycle graph is enforced in `domain/lifecycle.py` for user transitions, commands, scheduler transitions, and operator replay. Branches into clarification or Harness² probing store the interrupted return stage. User commands require an idempotency key, a reason, project authorization, and the observed task version (`If-Match`). A stale version returns HTTP 409 with `code=version_conflict` and `current_version`. Reusing an idempotency key for a different command returns HTTP 409.

Every state mutation writes a `TaskTransition` audit row and a versioned `DomainEvent`. The corresponding `OutboxMessage` is inserted in the same PostgreSQL transaction. Command replay returns the existing task result without repeating a mutation. System transitions use the same lifecycle guards and persisted audit/event shape; scheduler-owned stages cannot be forged through the user transition endpoint. Active pause/cancel commands update run status immediately, which prevents further dispatch, and call the execution adapter's cancellation hook for a current run.

The durable event envelope carries event and schema versions, organization/project/task/run scope, aggregate and project sequences, actor, occurrence time, correlation/causation IDs, and payload. Errors use the stable `code`, `message`, `request_id`, and `details` contract. Specification revision and artifact metadata schemas remain in the generated API contracts for later milestones.

## PostgreSQL and Redis orchestration

Alembic revision `ffac3c1be398` follows `0001_foundation`. It creates `task_transitions`, `runs`, `run_checkpoints`, `task_leases`, `domain_events`, `outbox_messages`, `consumer_inbox`, `effect_intents`, and `dead_letters`; it adds task priority, retry availability/cancellation fields, project concurrency limits, and the project event sequence. Milestone 5 revision `5c0a4b6d1e9f` adds an internal per-task event sequence and the fenced `run_artifacts` metadata table. Existing organization/user/membership/project/repository/task data remains intact when migrations are applied. IDs are UUIDs, timestamps are timezone-aware, and project/task scoped keys have indexes and uniqueness constraints.

The API writes state and outbox rows atomically. The publisher leases outbox rows in short transactions and then calls Redis outside the database transaction. A crash can produce a duplicate `XADD`, so consumers use a unique `(consumer, event_id)` inbox insert before acknowledging. A crash after inbox commit but before `XACK` is safely redelivered and deduplicated. Redis is not dispatch authority: schedulers query PostgreSQL `READY` tasks, and `/ops/dispatch/rebuild` reconstructs the Redis dispatch projection from retained, still-queued task state.

Event replay is HTTP-based at `GET /projects/{project_id}/events?after=...&limit=...`. The monotonically increasing project cursor yields stable ordering, is authorized by project membership, and is bounded by `EVENT_REPLAY_LIMIT` (default 500). Event retention is bounded by `EVENT_RETENTION_DAYS` (default 30); cursors older than retained events receive HTTP 410. Redis Stream length is bounded by `REDIS_STREAM_MAXLEN` (default 10,000). There is no SSE/AG-UI stream subscription in this milestone.

## Evidence provenance projection

`GET /projects/{project_id}/evidence` joins project-authorized native PostgreSQL records by their existing identifiers and emits the versioned `CoreEvidenceEnvelope`; it does not become a second task or event authority. It supports task/run/source/revision filters and a bounded cursor page. Domain event rows outside their `retention_until` boundary are omitted. Prompts, completions, raw webhook payloads, MCP arguments/results and artifact bytes are excluded. Unknown cost and absent identifiers stay explicitly unknown. `POST /projects/{project_id}/evidence/artifacts/{artifact_id}/reconcile` verifies one content-addressed artifact under a 64 MiB read bound and appends an idempotent domain audit event; it never changes task/run state or republishes the artifact. See `docs/integrations/evidence-plane.md` for the source matrix and the supported gaps/reconciliation limits.

The PostgreSQL scheduler uses a transaction-scoped advisory lock around claim accounting, row locks with `SKIP LOCKED`, and counts active leases against the global limit and each project's `max_concurrency`. It orders candidates by least recently dispatched project, then descending task priority, age, and ID. A claim atomically creates a run, increments the task's monotonic fencing token, establishes a renewable lease, advances the task to `IN_PROGRESS`, and appends the transition/event/outbox rows. The transaction commits before `ExecutionAdapter.execute` starts. Every heartbeat, checkpoint, completion, and failure checks owner, run ID, token, and lease expiry, so a stale worker cannot write over a replacement worker.

Checkpoints persist JSON state, a SHA-256 digest, sequence, run/task/project scope, resumability, and fencing token. The milestone 4 NO-GO keeps this scheduler as the only run-level authority. Pinned OpenCode advertises ACP session resume, but Harnessa does not persist its session store across container cleanup. Harnessa checkpoints contain protocol phase, exact base commit, pinned image, advertised capabilities/agent version, resource bounds, and redacted update counts; recovery deliberately starts a fresh session with that bounded context. An expired ACP run starts a new fenced attempt and ACP session from the same base commit plus that bounded metadata; it does not restore the old conversation or working directory. A stale lease cannot publish a checkpoint or artifact. Without a usable checkpoint, recovery marks the run visibly failed and creates a dead letter instead of launching a possible duplicate. Retryable transient and rate-limited failures use capped exponential backoff with jitter (0.8–1.2 multiplier); explicit provider `Retry-After` is bounded to one hour. Attempts default to three and total wall time defaults to 1,800 seconds. Permanent failures and exhausted limits are dead-lettered. Operators can inspect project dead letters and replay a failed task with a reason and idempotency key.

Effect intents use a stable external-effect key and a reconciliation protocol that distinguishes `applied`, `not_applied`, and `unknown`. Adapter calls run outside the database transaction. An abandoned `reconciling` intent is reclaimed after 60 seconds; unknown outcomes use bounded retry delay. Until an effect provider adapter exists, the worker leaves intents retryable and performs no external write.

## HTTP, identity, and access

FastAPI Users provides local password hashing and cookie sessions. Administrator bootstrap is one-time and requires `BOOTSTRAP_TOKEN`; production startup requires a 32-character secret, a bootstrap token, and Secure cookies. Mutations require an exact allowlisted Origin and double-submit CSRF token. Organization and project roles are resolved from local memberships. The internal identity contract is designed for a later OIDC adapter; provider claims do not replace local membership authority.

Milestone 2 adds authorized task creation/list/detail, stage transition, pause/resume/cancel/retry commands, run and transition history, project event replay, dead-letter inspection/replay, and administrator dispatch rebuild endpoints. Each task, run, dead letter, and event endpoint resolves its owning project and checks membership before returning data. Milestone 5 publishes artifact metadata only through the current run fence and emits redacted durable events; public artifact retrieval remains unimplemented and must use the same project authorization boundary.

Repository registration accepts only exact target IDs in `REPOSITORY_TARGETS`; it performs no GitHub connection, clone, fetch, or scan. LiteLLM, OpenSpec, AG-UI, Harness², external artifact storage, and GitHub integrations remain separate boundaries. ACP uses a pinned client and sandbox; the optional broker maps OpenAI-compatible model requests into the existing Harnessa ModelRouter and is gated by explicit host-worker configuration.

## Model routing and spend controls

PostgreSQL owns provider-account references, deployment capabilities and prices, alias fallback order, workload-pool membership, project budget policies, usage counters, reservations, and request ledger events. The API selects a route only when its protocol, streaming, tools, usage, cancellation, and context requirements are all satisfied. Fallback order is explicit in the alias/deployment rows. LiteLLM Proxy receives one selected deployment for each HTTP attempt; proxy retries and fallbacks are disabled so the application can audit every attempt. A route requiring unsupported behavior fails with `no_authorized_compatible_route` rather than being downgraded.

Each request locks its project budget policy and workload-partitioned project/task/run counters before dispatch, reserving the maximum cost and tokens across its configured bounded attempts. Interactive/primary and probe pools have independent project/task/run cost and token ceilings; the shared project policy lock serializes counter changes while every request remains inside its own pool ceilings. Capacity is tracked on an organization-wide interactive pool row and on each deployment. Probe requests are limited to `max_concurrency - interactive_reserve`, plus any explicit probe overflow; the capacity check retains an equivalent number of interactive slots. Expiring reservations are heartbeated while a stream is consumed and reconciled conservatively by the worker after process loss.

The ledger stores route, provider account, deployment, actual model, LiteLLM request ID, price revision, token usage, cost, latency, attempt decisions, and sanitized error codes. It never stores prompts, response text, or provider credentials. LiteLLM message logging is disabled. Provider key values are passed to the LiteLLM container only; API and worker services receive no provider keys. Compose gives only LiteLLM a non-internal egress network, and it does not publish the gateway port to the host. The default `infra/litellm/config.yaml` contains two static model aliases; health checks and setup diagnostics remain credential-free, while organization provider accounts, deployment capabilities, and pool membership still require administrator registration.

For non-streaming requests, transport retries are bounded and record each attempt before settling the reservation. A stream may fall back only before its first body line; after the response begins, failures are surfaced as `model.error` and never retry the model call or replay agent workflow/tool effects. Live compatibility checks are explicit administrator requests with an explicit paid-call acknowledgment and project budget reservation. Short-lived per-run runner credentials, agent execution, and workflow orchestration remain for later milestones.

## Later integration boundaries

The server-owned `ModelRouter` port is project-scoped and supports request and stream operations. The ACP broker authenticates a per-run token, checks the live fencing token, pins the selected model alias outside the agent request, and delegates accounting, route compatibility, cancellation, and the redacted ledger to this port. A static TCP-to-Unix relay inside the sandbox binds only to loopback; the container remains on `--network=none`. The broker and provider route are disabled by default. Clarification remains reserved for milestone 6; AG-UI may project durable run events independently. Artifact retrieval must follow project membership before any API route is added.

## Local deployment and operational limits

`infra/compose.yaml` boots PostgreSQL, Redis, API, web, worker, and LiteLLM. The API applies Alembic migrations before it serves requests; the worker waits for the healthy API and its schema. `.env.example` contains placeholders and disabled ACP defaults. Rootless Podman execution requires a separate host-side worker with its own trusted engine access; no engine socket is mounted into the agent. The sandbox receives the per-run workspace and, when the broker is enabled, only its scoped socket plus read-only broker config/relay mounts. It runs with `--network=none` and receives no provider credentials. No paid model call or provider-backed live edit was performed for milestone 5.
