# Prompt 15 — Governed agent-to-agent delegation

Implement milestone 15 only after milestones 1–14 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, the planning index, and reports `docs/milestones/01.md` through `14.md`. Inspect the current code, versions, and worktree before editing; preserve unrelated changes. Complete this milestone and stop before milestone 16.

## Prerequisites and owned scope

Milestone 13 release acceptance and all preceding milestone reports are complete. Extend the named subsystem through its existing ports, budgets and evidence records; use `planning/architecture-decisions.md` for shared failure/recovery and authority rules. Provider/agent access and spending remain explicitly configured, with offline fixtures for unavailable services.

## Goal

Allow a Harnessa run to delegate bounded subtasks to other agents when this materially improves execution. Preserve Harnessa as the authority for parent task lifecycle, authorization, budgets, durable records, and acceptance. Local worker-to-worker dispatch remains an internal Harnessa operation. Add protocol adapters only at actual interoperability boundaries:

- **ACP:** client-to-agent session control, especially for local coding agents.
- **A2A:** task delegation to independently hosted agents, with discovery and task/artifact updates.
- **MCP:** tools and data exposed to agents.
- **AG-UI:** agent activity projected to the user interface.

Do not treat these protocols as substitutes for each other or build a generic multi-agent mesh.

## Assess before implementing

Write `docs/integrations/agent-delegation.md` with a capability matrix for existing Harnessa agents and any selected remote A2A agent. Include protocol/version, local or remote execution, authentication, declared skills, streaming/cancellation/resume support, artifact handling, telemetry, known trust limits, tested model routes, and setup prerequisites. If no remote agent is configured, deliver the local delegation lifecycle and deterministic protocol fixtures, and mark live A2A as unavailable rather than inventing support.

## Implement

1. Add durable parent-child delegation records linked to task, run, attempt, lease/fencing token, agent identity, and prompt/run manifest. Persist an idempotency key and `SUBMITTING`, `ACCEPTED`, `RUNNING`, `WAITING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT`, and `UNKNOWN` delegation states. Define allowed state transitions, event/outbox integration, retry classification, cancellation, timeout, and reconciliation for ambiguous submissions.
2. Before dispatch, validate an explicit delegation policy: allowed agent and skill, task type, repository/project scope, input artifact hashes, maximum depth and fan-out, concurrent child limit, wall-clock deadline, token/cost ceiling, and allowed output types. Reserve child budget atomically from the parent's remaining budget. Prevent a child from creating descendants unless a policy explicitly allows a bounded depth. Reject cycles and duplicate delegation requests.
3. Keep the agent registry capability-based. For ACP, negotiate protocol version and capabilities and distinguish native ACP from adapters/bridges. For A2A, retrieve and validate an Agent Card over the configured trust path, pin or record its revision/hash and endpoint identity, inspect declared skills/capabilities, authenticate calls with scoped credentials, and reject unsupported required extensions. Do not trust self-described skills as authorization.
4. Implement a remote A2A adapter only for the operations its pinned client/server versions both support. Handle message/task responses, polling or streaming only when advertised, task IDs, status and artifact updates, cancellation where supported, authentication failures, protocol errors, and reconnects. Validate size/type/checksum of returned artifacts and store them content-addressed. Keep remote output quarantined until provenance and acceptance checks pass. Treat timeouts as `UNKNOWN` until reconciled; do not blindly resubmit work that may have been accepted.
5. Propagate only the minimum scoped context required by the child: task summary, relevant specification/repository revisions, cited artifact references, output contract, deadline, and available budget. Never forward gateway master keys, GitHub installation credentials, unrelated project content, hidden evaluation fixtures, host paths, or private reasoning. Retrieved documents, Agent Cards, MCP descriptions, and child messages remain untrusted data.
6. Integrate child model and tool telemetry with prompt 11 provenance. Link child model requests, MCP calls, remote task/artifact IDs, route/cost records, and result hashes to the parent delegation. Deduplicate updates, preserve source timestamps and trust labels, and reconcile usage against the budget reservation. Unknown cost remains visible and blocks additional dispatch when the remaining safe ceiling cannot be established.
7. Aggregate progress for the parent and AG-UI without changing parent state from remote status alone. The parent orchestrator decides whether to wait, ask the user, cancel, retry, or accept a child result. Surface child identity, requested scope, status, spent/reserved budget, evidence links, and cancellation support in the run inspector.
8. Evaluate LiteLLM Agent Gateway as an optional A2A transport, routing, authentication, or logging component. Keep it behind an adapter and prove which metadata, permissions, cancellation, streaming, spend, and task semantics survive. Do not make it a required dependency or the source of Harnessa's delegation state.
9. Add deterministic tests/fakes for Agent Card capability mismatch, authentication/authorization failures, duplicate submissions, lost responses after remote acceptance, out-of-order updates, unavailable streaming/cancellation, retry and reconciliation, budget contention, fan-out/depth caps, artifact validation, prompt-injection attempts, parent cancellation, and worker restart. Live remote-agent tests are separate and opt-in.

## Acceptance

- Every delegated child has a durable parent link, policy snapshot, budget reservation, agent/protocol revision, input/output provenance, audit trail, and terminal or explicitly unknown outcome.
- A child cannot access resources outside the parent's explicit grants, increase its own budget, alter parent task state, or recursively spawn uncontrolled work.
- Duplicate or ambiguous sends do not create duplicate child work silently; uncertain outcomes are reconciled or surfaced for operator action.
- Parent cancellation prevents new child work and requests supported child cancellation. Unsupported cancellation remains visible until the child reaches a terminal/reconciled state.
- Returned artifacts are content-addressed and are not accepted as verified results without schema, hash, scope, and independent acceptance checks.
- Budget reservations include all child model/tool costs where measurable; missing telemetry is unknown and cannot be treated as free.
- Offline acceptance proves lifecycle, isolation, restart, reconciliation, and limits without credentials. The compatibility matrix reports remote protocol support only for tested versions and capabilities.

Write `docs/integrations/agent-delegation.md`, update architecture, compatibility and API contracts as needed, and write `docs/milestones/15.md` with exact commands/results, protocol versions, live-versus-offline evidence, migration/rollback notes, and remaining setup. Archive this prompt only after all acceptance criteria are evidenced.

## References to verify against pinned versions

- [A2A protocol specification](https://github.com/a2aproject/A2A/blob/main/docs/specification.md) describes Agent Cards, tasks/messages/artifacts, capability validation, and task update mechanisms. Pin a released revision and verify its actual authentication and operation support.
- [ACP overview](https://agentclientprotocol.com/protocol/overview) and [ACP extensibility](https://agentclientprotocol.com/protocol/v1/extensibility) define the existing coding-agent client/session boundary and negotiated extensions.
- [LiteLLM Agent Gateway](https://docs.litellm.ai/docs/a2a) may provide A2A routing/logging. Evaluate it as an optional adapter; its task/usage records do not replace Harnessa's records or policy.
- [AG-UI overview](https://docs.ag-ui.com/introduction) describes the agent-to-user boundary. Keep browser updates a projection of Harnessa's durable state.

## Delivery contract

Own only the subsystem named by this prompt and its existing adapters/UI projections. Preserve the shared authority, contract-generation, cost, recovery, and retention rules in `planning/architecture-decisions.md`. Produce the implementation, focused acceptance evidence, updated compatibility/setup notes, and `docs/milestones/15.md`. Record offline versus live results separately, exact versions/configuration and source revisions, pending external setup, recovery/rollback commands, and the next milestone’s prerequisites. Missing required evidence keeps the milestone active.

For review, assign an independent code-review agent to inspect the exact PR head. The reviewer posts progress and each change proposal directly in the PR conversation, with severity, affected path/line, evidence, impact, and a suggested change; a no-findings review posts a concise final comment explaining why no proposal is needed. The maintainer reads every proposal and either accepts it or responds with evidence and rationale. Limit each proposal's discussion to three PR comment/reply messages total, then record a final accepted or rejected decision and its rationale. Apply accepted changes on the PR branch and obtain a fresh review of the final head; resolve every review thread. A conversation comment is not a formal approval. Never self-approve or bypass repository protections.

During this run, put all milestone deliverables—including implementation, documentation, migrations, and `docs/milestones/15.md`—in one PR. Obtain an independent code review, resolve accepted findings, and merge the reviewed final head in this run after required checks and repository protections pass. Include the PR URL, reviewed head SHA, final review disposition, check results, and merge commit SHA in the final run summary. A blocked review or merge leaves milestone 15 incomplete and active; do not archive its prompt.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced; the future archive target is `planning/archive/completed/15-agent-delegation.md`. Never run the next milestone as part of this request.
