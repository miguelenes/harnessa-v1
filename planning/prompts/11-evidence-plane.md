# Prompt 11 — Cross-system evidence and result provenance

Implement the first post-release evolution milestone for Harnessa only after prompt 10 is integrated and `docs/milestones/10.md` records its completion evidence. Read `planning/revised-specification.md`, `planning/README.md`, all applicable prompts, `docs/architecture.md`, `docs/compatibility.md`, and `docs/milestones/01.md` through `10.md`. Inspect the current code and live repository state before changing anything. Preserve unrelated work and do not assume any external integration is complete unless its evidence exists.

## Goal

Make every consequential result explainable across the Harnessa task/run, model gateway, execution harness, UI stream, GitHub effects, retrieval, and evaluation boundaries. Build on PostgreSQL domain events, outbox/inbox, effect intents, immutable revisions, and artifact metadata. Do not create a second workflow engine or treat any external system as Harnessa's authority for task state.

## Integration assessment

Before implementation, write a concise current-state matrix in `docs/integrations/evidence-plane.md`. For each source, list the authoritative record, identifiers available, delivery/replay behavior, missing links, and exact adapter boundary:

- **LiteLLM:** deployment/model, gateway request ID, route/fallback, token/cost/usage certainty, tags/metadata, and model/tool request logs. The gateway is authoritative for its request telemetry; Harnessa owns task budgets, reservations, and reconciliation. Do not store prompts or completions in logs by default.
- **Harness²:** pinned source revision, executor/substrate, benchmark/task identity, recursion mode and `k`, candidate component tree, probe executions, composition, adoption, judge/evaluation result, and run artifacts. Treat upstream result files as imported evidence with checksums and provenance, not mutable Harnessa state. Verify actual upstream layout and supported executor/benchmark capabilities against the pinned revision.
- **GitHub:** installation/repository identity, `X-GitHub-Delivery`, event/action, PR/check/revision/head SHA, outbound effect intent, API result, and reconciliation status. Webhooks are notifications, not authority to change task state without current-head and authorization checks.
- **AG-UI:** map durable domain/run events to protocol events and record stream cursor/replay information. AG-UI is a browser-facing projection; it never authorizes a mutation or replaces durable event storage.
- **MCP and skills:** record the server/tool identity and revision or digest, authorization scope, invocation/result references, and skill source/revision used to construct the agent context. Never treat retrieved or tool-returned text as policy.

## Implement

1. Define a versioned evidence envelope and typed links among organization, project, repository, task, task revision, run, attempt, model request, tool invocation, webhook delivery, Git effect, artifact, evaluation, harness revision, and skill revision. Include stable IDs, source, occurred/recorded times, correlation/causation, idempotency key where applicable, content hash, sensitivity/redaction state, and schema version. Use nullable/unknown values instead of fabricated defaults.
2. Implement adapters that ingest metadata and artifact references from LiteLLM, Harness², GitHub, and AG-UI without conflating their IDs or trust levels. Deduplicate repeated imports and webhook deliveries. Retain raw payloads only when required, encrypted/access-controlled and under explicit retention; default to minimized metadata and redacted content.
3. Preserve exact request, attempt, and result semantics: one agent run may produce multiple model requests, retries, MCP calls, and Git effects. Track provider-reported, gateway-recorded, estimated, reconciled, and unknown spend separately. Do not equate stream events, webhook deliveries, model calls, or benchmark scores with completed task outcomes.
4. Add read APIs and a provenance view to the existing event/task inspector. Support filters by task/run/source/revision and show evidence gaps, stale head/revision, redaction, import status, and reconciliation status. Use project authorization and cursor-based pagination. Do not add raw chain-of-thought display or expose secrets.
5. Add operational reconciliation for missing/late gateway usage, webhook delivery replay, artifact checksum mismatch, and failed imports. Reconciliation must be idempotent, bounded, auditable, and unable to repeat external writes. Preserve original evidence when a corrected/reconciled record arrives.
6. Document retention/deletion, export, backup/restore, schema evolution, and compatibility contracts. Provide deterministic fixtures for adapters; live provider/GitHub evidence is optional and must be labeled separately.

## Acceptance

- A complete task can be traced from its READY specification/base SHA through agent run, all model requests and tool calls, GitHub PR/check events, AG-UI activity projection, verification evidence, evaluation, and terminal outcome.
- Replayed webhooks, duplicate gateway records, stream reconnect/replay, worker retry, and repeated imports do not duplicate domain transitions or external effects.
- Every displayed result names its source and exact revisions/SHAs; stale or unknown evidence is visibly marked and cannot satisfy a current acceptance gate.
- Budget reconciliation distinguishes gateway/provider actuals from Harnessa reservations and estimates; missing usage remains unknown and alerts do not fabricate zero spend.
- Redaction and authorization checks prove that one project cannot inspect another project's payloads or artifacts and that secrets/prompts are excluded by default.
- Backup/restore preserves evidence links and checksums; a deterministic local acceptance path runs without paid credentials.

Write `docs/integrations/evidence-plane.md`, update architecture/compatibility docs and API contracts as needed, and write `docs/milestones/11.md` with exact commands/results, limitations, migration/rollback steps, and remaining provider setup. Archive this prompt only after all acceptance criteria have evidence; otherwise keep it active and record the state.

## Reference contracts to verify at implementation time

- [LiteLLM gateway and spend logs](https://docs.litellm.ai/docs/simple_proxy), [Harness² upstream](https://github.com/google-research/harness2), [GitHub webhook best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks), [GitHub signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries), and [AG-UI event model](https://docs.ag-ui.com/sdk/python/core/events).
- Pin versions and verify current interfaces/capabilities against those versions. Documentation links are discovery references, not runtime compatibility guarantees.
