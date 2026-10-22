# Prompt 4 of 13 — Run orchestration decision and compatibility spike

Implement milestone 4 only after milestones 1–3 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, current scheduler/recovery/effects/streams code, adapter ports, and milestone reports. Inspect dirty work without overwriting it. A draft milestone 3 report with pending required checks does not open this gate. Complete this milestone and stop before milestone 5.

## Prerequisites and owned scope

The durable task lifecycle and model gateway are integrated and evidenced. Own the runtime comparison, compatibility spike, core adapter/evidence contracts and ADR. Do not launch coding work, install a new production workflow service, or migrate live run state in this milestone. Use disposable databases and deterministic execution/model/effect fixtures; no paid provider or GitHub access is needed.

## Implement

1. Inventory current runtime responsibilities and data ownership: task/run/attempt, scheduler claims and leases, fencing, checkpoints, retries, cancellation, questions/answers, budget reservations, event/outbox/inbox and effect reconciliation. Record baseline source revision, configuration, schema, resource usage and recovery timings. Preserve Harnessa domain authority and the existing API/TypeScript generation pipeline.
2. Pin a Python 3.12-compatible LangGraph candidate and its compatible PostgreSQL checkpointer in an isolated spike dependency set. Verify license, exact APIs, serialization and migration behavior from official docs and Context7. Compare against the existing runtime using the same input and fault schedule. Never use in-memory checkpoint success to claim durable parity.
3. Define one typed run-orchestration port with start, checkpoint, interrupt/answer, resume, cancel and reconcile behavior; specify unsupported operations and duplicate requests. Map Harnessa run/attempt/fencing IDs to graph thread/checkpoint IDs. Keep library types inside the adapter. Establish the core evidence envelope from shared decisions before producing spike results.
4. Exercise the conformance matrix against both runtimes: crash before/after checkpoint; worker lease loss and stale publication; duplicate dispatch; restart during human wait; duplicate/stale answers; cancellation and process cleanup; bounded repair/retry; concurrent budget reservation; timeout after external effect acceptance; partial stream; event replay; database outage; schema upgrade and rollback. External effects use Harnessa intent records and reconciliation, not unguarded graph nodes.
5. Measure paired outcomes on a fixed disclosed workload: correctness, recovery and dispatch latency, database writes/storage, worker resource use, operational dependencies and custom orchestration code required. Declare workload and measurement thresholds before candidate trials; require zero critical semantic failures, no relaxation of budget/authority rules, and evidence of reduced maintenance complexity. Report tradeoffs and uncertainty; subjective framework preference alone cannot pass the gate.
6. Write `docs/adr/0004-run-orchestration.md` with a binary GO/NO-GO decision and per-case evidence. GO requires all critical cases to pass and a demonstrated maintenance benefit with accepted measured operational costs. Missing or inconclusive critical evidence means NO-GO and retains the current scheduler; this is a valid completed milestone outcome. Do not leave framework selection to the next implementer.
7. For GO, include a concrete single-owner integration/migration plan for milestone 5: checkpoint tables/schema owner, domain-event projection, atomic effect boundary, versions, new-run rollout, drain of old runs, backup/restore and rollback. Default to draining active runs rather than converting live checkpoints. For NO-GO, document the gaps and keep the existing execution port. Milestone 5 implements only the selected runtime path; production migration needs the milestone 4 decision and its proven preconditions.

## Acceptance and outputs

- Both runtime paths have reproducible offline conformance evidence on disposable real PostgreSQL; no duplicate effect or stale publication is accepted.
- The ADR records GO or NO-GO, ownership mapping, exact versions, measured tradeoffs and executable reproduction commands. NO-GO is not a fabricated LangGraph integration success.
- Evidence/adapter contracts are typed and do not expose framework internals through public APIs. No production task, checkpoint or gateway configuration is migrated by the spike.
- Write `docs/milestones/04.md` with the decision, outputs, cost/resource bounds, fixture versus live limitations and exact prerequisite checks for milestone 5. Keep all earlier completion status unchanged.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced. The future archive target is `planning/archive/completed/04-runtime-decision.md`.
