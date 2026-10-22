# Prompt 2 of 10 — Durable orchestration and task lifecycle

Implement milestone 2 of Harnessa. Read `planning/revised-specification.md`, `docs/architecture.md`, and `docs/milestones/01.md`. Preserve existing behavior and unrelated edits. Complete this milestone and stop before milestone 3.

## Prerequisites

Milestone 1 is accepted. Fix any foundational blocker needed for this milestone and document it instead of building around an invalid contract.

## Implement

1. Build the single domain transition service covering all ten board stages and their legal branches. Include guard reasons, actor authorization, expected task version, and audited manual commands. Model pause/failure/cancellation separately from the board stage. Preserve the return stage for clarification and probe branches.
2. Create migrations for transition history, runs, checkpoints, leases/fencing tokens, durable domain events, transactional outbox, consumer inbox, effect intents, and dead letters. State mutation and its outbox event must commit atomically.
3. Implement outbox publication to Redis Streams and consumer-group processing with acknowledgments after durable handling. Store consumer deduplication transactionally. Rebuild dispatch from retained database state if Redis data is lost. Use bounded retention with explicit replay limits.
4. Implement a worker scheduler with global/project concurrency controls, priority/fairness, atomic READY claims, renewable leases, heartbeat deadlines, monotonically increasing fencing tokens, checkpoint recovery, and rejection of stale-worker writes. Do not hold database transactions open during agent execution.
5. Add retry classes, exponential backoff/jitter, attempt and wall-time limits, dead-letter inspection/replay, pause/resume/cancel, and a reconciliation loop for effect intents whose outcome is unknown. Cancellation must prevent new dispatch immediately and request cancellation of active work through an adapter.
6. Expose task creation, listing, detail, transition, run history, and operator recovery APIs. Add a durable event replay endpoint with project-scoped cursors and stable ordering. Authorize every request and stream subscription.

## Acceptance

- Concurrent claims yield one current lease owner; an expired worker cannot advance a task after reassignment.
- Duplicate events and replayed commands do not repeat domain effects.
- A crash between database commit and Redis publish loses no task; a crash after handling but before acknowledgment does not duplicate the result.
- Worker restart resumes from a checkpoint or visibly marks an unrecoverable run, without silently launching duplicate agents.
- Illegal transitions, stale task versions, exhausted retries, cancellation, and Redis rebuild are covered by focused integration/fault tests against real PostgreSQL and Redis.

Use a deterministic fake execution adapter for these tests; label it as a test adapter. Do not add simulated agent activity to production behavior. Update API contracts and write `docs/milestones/02.md` with invariants, recovery procedures, commands/results, and the hooks later adapters must implement.

## Archive this planning step

After every acceptance criterion above is met and `docs/milestones/02.md` records actual commands/results, limitations, and the next milestone's prerequisites, move this prompt from `planning/prompts/` to `planning/archive/completed/` without changing its contents. Update the matching milestone row in `planning/README.md` to link to the archived prompt and mark it **archived complete**. Do this only after the milestone is complete; if it is blocked or partial, keep this prompt active and record the state and evidence in the milestone report.
