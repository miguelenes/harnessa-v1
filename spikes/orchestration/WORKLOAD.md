# Orchestration spike workload and thresholds

**Declared before candidate trials:** 2026-10-22

**Baseline source:** `8ddc86e77520288e2bfc9699d3ad7b67fe2e6b03`
**Purpose:** Compare the existing Harnessa scheduler/execution boundary with one isolated LangGraph + PostgreSQL checkpointer candidate. This is a decision spike, not a production rollout.

## Fixed environment and workload

- Python 3.12; PostgreSQL 16.10; Redis 7.4.2.
- Disposable local containers only. PostgreSQL: 2 CPU, 1 GiB memory, 512 MiB temporary data volume. Redis: 1 CPU, 256 MiB. Test runner: at most 2 CPU and 1 GiB.
- No paid model, GitHub API, agent CLI, production service, or live Harnessa database.
- Per measured sample: 64 runs, 4 concurrent workers, 4 deterministic steps per run, 3 durable checkpoints per run, 256-byte fixed input, 128-byte fixed result. Sixteen runs pause at one human-input boundary. Every run attempts one stable-key external effect through a deterministic acceptance/reconciliation fixture. Harnessa authority records are the common source for task/run/attempt identity, lease/fencing, events, budgets, questions, and effects.
- Eight warm-up runs, then three measured samples per runtime. Alternate runtime order by sample. Report median and p95 dispatch-to-first-step and recovery latency; report PostgreSQL transaction counts, table rows/bytes, and peak runner RSS. The deterministic conformance faults are run separately from these timing samples and are not mixed into latency measurements.

## Decision thresholds fixed before candidate execution

All criteria are conjunctive for GO:

1. Every critical conformance case passes on both runtimes. Zero duplicate accepted effects, stale publications, duplicate question answers, or authority/budget bypasses.
2. Candidate p95 dispatch and crash-recovery latency are each no more than 1.25 times the current runtime. Report absolute values and sample spread.
3. Candidate PostgreSQL writes and checkpoint storage are each no more than 2 times the current runtime for the fixed workload.
4. Candidate peak worker RSS is no more than 1.5 times the current runtime and no more than 256 MiB per worker. It adds no production service dependency.
5. The candidate reduces maintained custom run-step/checkpoint/interrupt code by at least 20%, measured from the exact source files and function boundaries recorded in the ADR. Harnessa claims, leases, fencing, events, budgets, question/answer authority, external-effect intent, and reconciliation are excluded from removable code and must remain in Harnessa.
6. Checkpoint schema upgrade and the documented rollback route both complete on disposable PostgreSQL without losing Harnessa authority/audit rows or replaying accepted effects.

Any missing, unsupported, or inconclusive critical case is a NO-GO. These thresholds cannot be relaxed after candidate results are observed.

## Critical conformance cases

Each case records the runtime, exact command/test, expected invariant, actual result, and whether it is supported by the current product surface or only by a test fixture:

1. Process crash immediately before and immediately after a durable checkpoint.
2. Lease expiry during execution; stale worker checkpoint/completion publication is rejected by the current fence.
3. Duplicate dispatch for one idempotency key does not create a second attempt or effect.
4. Process restart while a run is waiting for a human answer.
5. Duplicate, stale, and wrong-revision answer submissions are rejected or replayed idempotently by Harnessa authority.
6. Cancellation reaches the runtime and worker/process resources are cleaned up.
7. Bounded retry/repair stops at attempt and wall-time limits.
8. Concurrent budget reservations cannot exceed project, task, or run ceilings.
9. Timeout after an external effect is accepted is reconciled by stable key without repeating the effect.
10. Partial output streams remain visible as partial and never restart an already executed effect.
11. Retained Harnessa event replay returns ordered events without dispatch authority.
12. Database outage prevents durable progress/publication; recovery resumes only from committed state.
13. Checkpoint schema setup/upgrade is repeatable, and the documented rollback path preserves Harnessa state and audit evidence.

Where a feature does not exist in the current product, exercise the public/API boundary or adapter contract and mark it unsupported; do not substitute a synthetic pass for missing Harnessa authority.
