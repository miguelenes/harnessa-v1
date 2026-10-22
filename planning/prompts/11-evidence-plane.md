# Prompt 11 of 13 — Cross-system evidence and result provenance

Implement milestone 11 only after milestones 1–10 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, the planning index, and reports `docs/milestones/01.md` through `10.md`. Inspect the current code, versions, and worktree before editing; preserve unrelated changes. Complete this milestone and stop before milestone 12.

## Prerequisites and owned scope

All milestones through 10 have completed acceptance evidence. The existing task, authorization, artifact and model-budget services are operational. Extend the named subsystem through its current ports; use the shared decisions for failure/recovery, retention and cost rules. External services require configured access; deterministic fixtures cover unavailable integrations.

## Goal

Make every consequential result explainable across the Harnessa task/run, model gateway, execution harness, UI stream, GitHub effects, retrieval, and evaluation boundaries. Build on PostgreSQL domain events, outbox/inbox, effect intents, immutable revisions, and artifact metadata. Do not create a second workflow engine or treat any external system as Harnessa's authority for task state.

## Integration assessment

Before implementation, write a concise current-state matrix in `docs/integrations/evidence-plane.md`. For each source, list the authoritative record, identifiers available, delivery/replay behavior, missing links, and exact adapter boundary:

- **LiteLLM:** deployment/model, gateway request ID, route/fallback, token/cost/usage certainty, tags/metadata, and model/tool request logs. The gateway is authoritative for its request telemetry; Harnessa owns task budgets, reservations, and reconciliation. Do not store prompts or completions in logs by default.
- **Harness²:** pinned source revision, executor/substrate, benchmark/task identity, recursion mode and `k`, candidate component tree, probe executions, composition, adoption, judge/evaluation result, and run artifacts. Treat upstream result files as imported evidence with checksums and provenance, not mutable Harnessa state. Verify actual upstream layout and supported executor/benchmark capabilities against the pinned revision.
- **GitHub:** installation/repository identity, `X-GitHub-Delivery`, event/action, PR/check/revision/head SHA, outbound effect intent, API result, and reconciliation status. Webhooks are notifications, not authority to change task state without current-head and authorization checks.
- **AG-UI:** map durable domain/run events to protocol events and record stream cursor/replay information. AG-UI is a browser-facing projection; it never authorizes a mutation or replaces durable event storage.
- **MCP and skills:** record the server/tool identity and revision or digest, authorization scope, invocation/result references, and skill source/revision used to construct the agent context. Never treat retrieved or tool-returned text as policy.

## Core evidence before framework evolution

Extend the core evidence envelope defined before execution rather than introducing a new event authority. Existing task, request, invocation and artifact IDs remain stable. Complete core delivery/GitHub/AG-UI joins now; provide versioned fixture contracts for later Harness², skill, A2A and Jev sources. Later milestones add real adapters and acceptance traces as their integrations land. Never require future milestone execution to complete this milestone.

## Implement

1. Extend and version the shared core evidence envelope and typed links among organization, project, repository, task, task revision, run, attempt, model request, tool invocation, webhook delivery, Git effect, artifact, evaluation, harness revision, and skill revision. Include stable IDs, source, occurred/recorded times, correlation/causation, idempotency key where applicable, content hash, sensitivity/redaction state, and schema version. Use nullable/unknown values instead of fabricated defaults.
2. Implement adapters that ingest metadata and artifact references from LiteLLM, GitHub, and AG-UI; define fixture-tested Harness² import contracts for milestone 12 without conflating their IDs or trust levels. Deduplicate repeated imports and webhook deliveries. Retain raw payloads only when required, encrypted/access-controlled and under explicit retention; default to minimized metadata and redacted content.
3. Preserve exact request, attempt, and result semantics: one agent run may produce multiple model requests, retries, MCP calls, and Git effects. Track provider-reported, gateway-recorded, estimated, reconciled, and unknown spend separately. Do not equate stream events, webhook deliveries, model calls, or benchmark scores with completed task outcomes.
4. Add read APIs and a provenance view to the existing event/task inspector. Support filters by task/run/source/revision and show evidence gaps, stale head/revision, redaction, import status, and reconciliation status. Use project authorization and cursor-based pagination. Do not add raw chain-of-thought display or expose secrets.
5. Add operational reconciliation for missing/late gateway usage, webhook delivery replay, artifact checksum mismatch, and failed imports. Reconciliation must be idempotent, bounded, auditable, and unable to repeat external writes. Preserve original evidence when a corrected/reconciled record arrives.
6. Document retention/deletion, export, backup/restore, schema evolution, and compatibility contracts. Provide deterministic fixtures for adapters; live provider/GitHub evidence is optional and must be labeled separately.

## Acceptance

- A complete core-delivery task can be traced from its READY specification/base SHA through agent run, all model requests and tool calls, GitHub PR/check events, AG-UI activity projection, verification evidence, available evaluation records, and terminal outcome. Harness² imports use deterministic fixtures until milestone 12 supplies real runs; missing future integrations are explicit gaps, not completion blockers for this core trace.
- Replayed webhooks, duplicate gateway records, stream reconnect/replay, worker retry, and repeated imports do not duplicate domain transitions or external effects.
- Every displayed result names its source and exact revisions/SHAs; stale or unknown evidence is visibly marked and cannot satisfy a current acceptance gate.
- Budget reconciliation distinguishes gateway/provider actuals from Harnessa reservations and estimates; missing usage remains unknown and alerts do not fabricate zero spend.
- Redaction and authorization checks prove that one project cannot inspect another project's payloads or artifacts and that secrets/prompts are excluded by default.
- Backup/restore preserves evidence links and checksums; a deterministic local acceptance path runs without paid credentials.

Write `docs/integrations/evidence-plane.md`, update architecture/compatibility docs and API contracts as needed, and write `docs/milestones/11.md` with exact commands/results, limitations, migration/rollback steps, and remaining provider setup. Archive this prompt only after all acceptance criteria have evidence; otherwise keep it active and record the state.

## Reference contracts to verify at implementation time

- [LiteLLM gateway and spend logs](https://docs.litellm.ai/docs/simple_proxy), [Harness² upstream](https://github.com/google-research/harness2), [GitHub webhook best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks), [GitHub signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries), and [AG-UI event model](https://docs.ag-ui.com/sdk/python/core/events).
- Pin versions and verify current interfaces/capabilities against those versions. Documentation links are discovery references, not runtime compatibility guarantees.

## Delivery contract

Own only the subsystem named by this prompt and its existing adapters/UI projections. Preserve the shared authority, contract-generation, cost, recovery, and retention rules in `planning/architecture-decisions.md`. Produce the implementation, focused acceptance evidence, updated compatibility/setup notes, and `docs/milestones/11.md`. Record offline versus live results separately, exact versions/configuration and source revisions, pending external setup, recovery/rollback commands, and the next milestone’s prerequisites. Missing required evidence keeps the milestone active.

During this run, put all milestone deliverables—including implementation, documentation, migrations, and `docs/milestones/11.md`—in one PR. Obtain an independent code review, resolve findings, and merge the reviewed head in this run after required checks and repository protections pass. Do not self-approve or bypass protections. Include the PR URL, reviewed head SHA, review outcome, check results, and merge commit SHA in the final run summary. A blocked review or merge leaves milestone 11 incomplete and active; do not archive its prompt.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced; the future archive target is `planning/archive/completed/11-evidence-plane.md`. Never run the next milestone as part of this request.
