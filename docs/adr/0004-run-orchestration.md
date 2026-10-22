# ADR 0004: Run orchestration runtime

- **Status:** Decided — **NO-GO for LangGraph; retain the Harnessa scheduler**
- **Date:** 2026-10-22
- **Decision scope:** milestone 4 only; no production runtime or run-state migration
- **Baseline:** `main` at `8ddc86e77520288e2bfc9699d3ad7b67fe2e6b03`
- **Evidence report:** [milestone 4](../milestones/04.md)
- **Workload and thresholds:** [declared before candidate trials](https://github.com/miguelenes/harnessa-v1/blob/main/spikes/orchestration/WORKLOAD.md)

## Decision

Keep `Scheduler` as the selected run runtime. The LangGraph candidate demonstrated durable PostgreSQL checkpoints, typed interrupt responses, resume after constructing a new saver/graph, and repeatable checkpoint-schema setup. It did not demonstrate parity with Harnessa's claims, fencing, event publication, model budgets, cancellation, external-effect reconciliation, or question/answer authority. Several required fault cases have no product boundary to exercise. The fixed paired workload and its latency, database-write/storage, RSS, and custom-code thresholds therefore remain unmeasured.

The thresholds in `WORKLOAD.md` are conjunctive. Missing or inconclusive critical cases force NO-GO; no threshold was relaxed. The spike does not add LangGraph to production dependencies, add a workflow service, alter run state, or change milestones 1–3.

## Context and current ownership

At the baseline revision, `apps/api/harnessa/services/scheduler.py` owns dispatch claims, run/attempt creation, PostgreSQL leases, monotonically increasing fencing tokens, checkpoints, heartbeat, cancellation acknowledgement, bounded retries, and expired-worker recovery. `services/streams.py` publishes/rebuilds Redis dispatch from the PostgreSQL outbox and deduplicates consumed events through the inbox. `services/effects.py` reconciles stable-key effect intents. `services/model_gateway.py` owns project/task/run model reservations, gateway retry/fallback, request evidence, and partial-stream handling. `adapters/execution.py` is the execution boundary; production uses `UnconfiguredExecutionAdapter`, so it does not execute work by default.

There is no durable question/answer model or question/answer API route in this baseline. `WAITING_FOR_INPUT` exists as a run status, but that alone is not a question authority. A real production execution adapter is also absent. The milestone 3 report and acceptance checks were re-read and re-run before the spike: lint, typecheck, unit suite, service readiness, contract generation, disposable PostgreSQL/Redis integration suite, fresh migration, upgrade-from-foundation data preservation, and `alembic check` passed. The optional paid-provider probe remains pending as described by milestone 3; no paid call was made here.

### Authority mapping

| Concern | Harnessa owner (required in either design) | LangGraph candidate boundary |
| --- | --- | --- |
| Task/run/attempt IDs and dispatch idempotency | PostgreSQL task/run rows, outbox/inbox and stable command keys | Adapter maps `Run.id` to `thread_id`; graph identifiers stay private |
| Lease, heartbeat, fencing, stale publication rejection | `TaskLease`, run fencing token and transactional scheduler checks | No lease or fence ownership; every adapter operation must carry Harnessa identity and be gated by it |
| Domain status, event order/replay, audit, evidence | Harnessa events, transitions and API replay | No domain-event authority; completion must be projected by Harnessa transactionally |
| Model budgets/reservations and cost | Model gateway and its durable reservations/ledger | No graph-level budget authority; graph nodes cannot bypass the gateway |
| Questions, answers, authorization and revisions | Harnessa must add the missing durable question/answer authority before either runtime can support them | `interrupt()` can pause/resume graph state only; answer acceptance and deduplication remain external |
| External effects and ambiguous outcomes | Harnessa `EffectIntent`, stable keys and reconciliation | Graph nodes must call Harnessa intent/reconciliation operations; replay cannot directly repeat an effect |
| Step execution and graph checkpoint history | Selected by the runtime decision, without duplicating mutable domain state | Candidate only: graph steps/checkpoints; not adopted |

The new `RunOrchestrator` protocol and Pydantic request/receipt/evidence models are neutral contracts. The candidate mapping is `thread_id = str(run_id)` for one Harnessa run attempt. The port exposes only Harnessa IDs, a Harnessa checkpoint reference, request idempotency keys, question revisions, and stable effect keys. It does not expose LangGraph `StateGraph`, `Command`, saver, interrupt, or checkpoint objects.

## Candidate and compatibility evidence

The isolated dependency set pins `langgraph==1.2.16` and `langgraph-checkpoint-postgres==3.2.0`; `uv.lock` resolves `langgraph-checkpoint==4.3.0`, `psycopg==3.3.6`, and `psycopg-pool==3.3.3`. Both selected packages declare MIT licensing and Python `>=3.10`; LangGraph's PyPI classifiers include Python 3.12. The pinned packages are compatible with Harnessa's Python `>=3.12,<3.13` target. No candidate package was added to the production manifest.

The compatibility test exercised the pinned APIs on Python 3.12.12 and PostgreSQL 16.10: `AsyncPostgresSaver.from_conn_string`, `async with`, `await setup()`, `StateGraph`, `interrupt(..., response_schema=...)`, and `Command(resume=...)`. A new saver and graph resumed a persisted interrupt using the same thread ID. The interrupted node ran again from its beginning, confirmed by the fixture attempt counter. The test passed with `LANGGRAPH_STRICT_MSGPACK=true` while the state contained JSON-safe values only. Production would need to keep strict msgpack deserialization or an explicit narrow allowlist; arbitrary Python objects are not part of the contract.

The PostgreSQL checkpointer owns tables including `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, and `checkpoint_writes`. On the disposable database, `setup()` was run twice successfully; `checkpoint_migrations` contained 10 rows through version 9. The pinned source applies migrations by appending entries to a forward-only `MIGRATIONS` list. The saver has no downgrade API, which the compatibility test checks. The spike did not claim that repeatable setup proves upgrade from an older production schema, nor that a rollback preserving Harnessa rows/effects is available. Checkpoint retention/pruning would also need an operational policy.

Official package metadata and tagged source were checked alongside Context7. Context7 resolved the LangGraph docs, but its version catalog only exposed releases through 1.0.8; current unversioned Context7 snippets were not treated as proof for 1.2.16. Exact-version behavior was checked against the 1.2.16 package/source and the executable PostgreSQL smoke test.

- [LangGraph 1.2.16 on PyPI: release, Python support and MIT license](https://pypi.org/project/langgraph/1.2.16/)
- [PostgreSQL checkpointer 3.2.0 on PyPI: Python support, MIT license and setup/security guidance](https://pypi.org/project/langgraph-checkpoint-postgres/3.2.0/)
- [Tagged checkpoint migration source](https://github.com/langchain-ai/langgraph/blob/1.2.16/libs/checkpoint-postgres/langgraph/checkpoint/postgres/base.py) and [async setup implementation](https://github.com/langchain-ai/langgraph/blob/1.2.16/libs/checkpoint-postgres/langgraph/checkpoint/postgres/aio.py)
- [Official interrupt guide](https://docs.langchain.com/oss/python/langgraph/interrupts) and [persistence guide](https://docs.langchain.com/oss/python/langgraph/persistence)

## Conformance matrix

Commands below ran on disposable services. “Partial” and “fixture-only” are not acceptance passes. The candidate had no Harnessa runtime adapter, so its isolated LangGraph behavior cannot establish domain-authority parity.

| Critical case | Current Harnessa evidence | LangGraph candidate evidence | Decision result |
| --- | --- | --- | --- |
| Crash before/after durable checkpoint | `test_expired_worker_without_checkpoint_fails_visibly_without_relaunch` and `test_concurrent_claim_and_expired_worker_fencing_with_checkpoint` simulate lease-loss recovery on each side of a checkpoint; they do not kill/restart a process at both boundaries. | The human-wait test closes and reconstructs the saver/graph after a persisted interrupt; it does not inject process death immediately before and after checkpoints. | Partial; not a full crash test on either path. |
| Lease loss and stale publication | `test_concurrent_claim_and_expired_worker_fencing_with_checkpoint` verifies fence increment and stale checkpoint/completion rejection. | No Harnessa lease/fence is integrated into the candidate. | Current passes; candidate missing. |
| Duplicate dispatch | `test_duplicate_commands_outbox_crash_ack_dedup_and_redis_rebuild` verifies inbox/outbox dedupe and Redis rebuild behavior. | `test_duplicate_dispatch_is_gated_by_authority_key_before_graph_invocation` passes only against a spike fixture table; no Harnessa dispatcher/claim is involved. | Current dispatch/event path passes; candidate is fixture-only. |
| Restart during human wait | No durable question entity or answer route exists. | `test_human_wait_survives_restart_and_harnessa_fixture_deduplicates_answer_and_effect` resumes a LangGraph interrupt after constructing a new saver/graph against PostgreSQL. | Candidate framework capability passes; Harnessa product case unsupported. |
| Duplicate, stale and wrong-revision answers | No question/answer authority exists in current API/database. | The same test's `spike_question_authority`/`spike_answer_receipts` fixture deduplicates one answer and rejects a stale revision/conflicting key. It is not Harnessa authority. | Fixture-only; critical authority evidence missing. |
| Cancellation and process cleanup | `test_active_cancel_is_forwarded_to_adapter_and_audited` passes adapter cancellation/audit. It does not assert OS-process cleanup. | No candidate worker adapter or cancellation cleanup test exists. | Partial current evidence; candidate missing. |
| Bounded retries/repair | `test_wall_time_limit_stops_retry_and_creates_dead_letter` and `test_retry_exhaustion_cancellation_commands_and_operator_replay` pass Harnessa bounds. | No candidate retry owner is wired to Harnessa attempt/wall-time bounds; not tested. | Current passes; candidate missing. |
| Concurrent budgets | Three parametrized `test_concurrent_reservations_cannot_exceed_project_task_or_run_limits` cases pass. | The graph candidate is not wired to gateway reservations or model calls; not tested. | Current passes; candidate missing. |
| Timeout after external effect acceptance | `test_external_effect_reconciliation_is_idempotent_and_retryable` tests stable-key reconciliation/retry, but not the exact timeout-after-accepted schedule. | The interrupt test verifies that node re-entry increments fixture attempts while a fixture stable key records one acceptance; it does not test Harnessa intent reconciliation or timeout-after-acceptance. | Partial on both; critical acceptance schedule missing. |
| Partial output stream | `test_partial_stream_failure_is_visible_and_never_replays_tools` passes model-gateway partial-stream evidence and proves no workflow replay. | Candidate has no model/stream path. | Current model boundary passes; candidate not exercised. |
| Retained event replay | `test_api_task_contracts_authorization_and_project_cursor_replay` passes ordered authorized Harnessa replay. | Candidate publishes no Harnessa events and has no replay API. | Current passes; candidate unsupported. |
| Database outage | No outage fault was injected into the current scheduler. | No outage fault was injected into the candidate. | Missing on both. |
| Checkpoint schema upgrade/rollback | Milestone 3 fresh/upgrade migration acceptance and `alembic check` pass for Harnessa schema; no milestone 4 checkpoint rollback preserving authority/effects was run. | Fresh setup and repeated setup pass at migration version 9. No older-version upgrade fixture or downgrade exists; authority/effect preservation on rollback was not demonstrated. | Critical schema rollback requirement missing. |

The current suite ran all 17 existing orchestration/model-gateway integration tests and passed. The candidate suite ran 3 compatibility tests and passed. This is a matrix review against available paths, not a claim that all 13 semantic cases passed. The gaps above are recorded as failures to meet the gate rather than synthetic successes.

## Measurements and thresholds

The workload and thresholds were committed to `spikes/orchestration/WORKLOAD.md` before LangGraph trial execution: 64 runs, 4 workers, 4 steps/run, 3 durable checkpoints/run, fixed 256-byte input and 128-byte result, 16 human waits, and one stable-key effect per run; 8 warmups plus 3 alternating measured samples. The disposable test environment used PostgreSQL 16.10 capped at 2 CPUs/1 GiB with 512 MiB tmpfs, Redis 7.4.2 capped at 1 CPU/256 MiB, and test-runner containers capped at 2 CPUs/1 GiB. Candidate and current fault-suite runs made no paid-model, GitHub, production-service, or live-state calls.

The full workload was **not** run. The current production adapter is deliberately unconfigured, and neither the current product nor candidate adapter can execute the 16 human waits through Harnessa question authority. Executing those waits through synthetic authority would violate the prompt. As a result there is no valid paired p50/p95 dispatch or crash-recovery measurement, PostgreSQL write/storage comparison, peak-worker-RSS comparison, or 20% custom-code reduction result. The 0.65-second candidate test-suite duration is a compatibility smoke-test duration and is not a workload latency measurement. Every performance/complexity threshold is therefore inconclusive and fails the GO gate.

Measured test cost was local only: no external provider spend, GitHub calls, or new service. Configured container limits above are maximums, not observed peak-use measurements. PostgreSQL test data lived only in the disposable 512 MiB tmpfs container. The production dependency and service footprint is unchanged.

## Reproduction

The following commands use a disposable PostgreSQL test user and database only. The candidate lock/environment is isolated from production dependencies.

```sh
# Disposable PostgreSQL (16.10) and Redis (7.4.2), with explicit resource caps.
podman run --detach --name harnessa-m4-pg-spike --network host --cpus 2 --memory 1g \
  --tmpfs /var/lib/postgresql/data:rw,size=536870912 \
  -e POSTGRES_USER=harnessa -e POSTGRES_PASSWORD=harnessa -e POSTGRES_DB=harnessa_test \
  docker.io/library/postgres@sha256:38471f330eb885e04de130b768d6db4e10469e2311879c7e5c699f6d2d8a1c74 \
  postgres -p 25434
podman run --detach --name harnessa-m4-redis --network host --cpus 1 --memory 256m \
  docker.io/library/redis@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952 \
  redis-server --port 26380
podman exec harnessa-m4-pg-spike pg_isready -h 127.0.0.1 -p 25434
podman exec harnessa-m4-pg-spike sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -p 25434 -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE harnessa_m4_candidate_test"'
podman exec harnessa-m4-pg-spike sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -p 25434 -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE harnessa_m4_current_test"'

uv lock --directory spikes/orchestration/candidate --python 3.12.12

# Candidate compatibility suite; PostgreSQL is PostgreSQL 16.10 at port 25434.
podman run --rm --network host --cpus 2 --memory 1g \
  -v "$PWD:/workspace:ro" \
  -e UV_PROJECT_ENVIRONMENT=/tmp/harnessa-m4-candidate-venv \
  -e M4_SPIKE_DATABASE_URL=postgresql://harnessa:harnessa@127.0.0.1:25434/harnessa_m4_candidate_test \
  -e LANGGRAPH_STRICT_MSGPACK=true \
  docker.io/library/harnessa-m3-api:latest sh -lc \
  'uv run --project /workspace/spikes/orchestration/candidate --locked --python /app/.venv/bin/python pytest -q -vv /workspace/spikes/orchestration/candidate/tests/langgraph_postgres_cases.py'

# Current Harnessa integration matrix; Redis uses its disposable database 1.
podman run --rm --network host --cpus 2 --memory 1g \
  -v "$PWD:/workspace:ro" \
  -e UV_PROJECT_ENVIRONMENT=/tmp/harnessa-m4-candidate-venv \
  -e TEST_DATABASE_URL=postgresql+asyncpg://harnessa:harnessa@127.0.0.1:25434/harnessa_m4_current_test \
  -e TEST_REDIS_URL=redis://127.0.0.1:26380/1 \
  docker.io/library/harnessa-m3-api:latest sh -lc \
  'uv run --project /workspace/spikes/orchestration/candidate --locked --python /app/.venv/bin/python pytest -q -vv -m integration /workspace/tests/integration'

pnpm lint:api
pnpm typecheck:api
pnpm test:api
pnpm typecheck:web
pnpm lint:web

# Remove only these spike-owned disposable containers after the run.
podman rm --force harnessa-m4-pg-spike harnessa-m4-redis
```

Actual results on this revision: candidate `3 passed` with strict msgpack and PostgreSQL; current integration suite `17 passed`; API unit suite `13 passed, 17 skipped` (integration tests are skipped without their explicit disposable-service variables); API lint/typecheck and web lint/typecheck passed. `pnpm contracts:generate` against the updated local OpenAPI server regenerated TypeScript contracts successfully.

## Consequences and follow-up

- Retain `Scheduler`, the current execution port, PostgreSQL authority, and Redis-as-rebuildable-transport design.
- The new neutral contract declarations are planning/adapter surface only; they do not create question endpoints or a production executor.
- Do not select LangGraph for milestone 5. Milestone 5 proceeds on the current scheduler path unless a later, separately authorized decision reopens this ADR with complete evidence.
- Before any future runtime re-evaluation, implement the missing Harnessa question/answer authority; build a candidate adapter behind the port; prove fencing, cancellation/process cleanup, external-effect timeout reconciliation, database-outage behavior, full schema rollback with authority/effect preservation, and the declared paired workload; then quantify custom-code reduction from comparable production adapter boundaries.
- If a future decision reaches GO, use a single-owner plan: keep Harnessa as owner of all domain state and effects; assign only graph step/checkpoint history to one adapter; route new runs to one runtime; drain old runs instead of translating live checkpoints; back up checkpoint and Harnessa schemas; and roll back by stopping dispatch, reconciling active/unknown effects, and restoring the previous compatible runtime without deleting published effects or audit rows. This migration is documented as a guardrail only and was not implemented.
