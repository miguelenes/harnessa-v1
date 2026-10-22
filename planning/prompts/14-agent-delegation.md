# Prompt 14 — Governed agent-to-agent delegation

Implement milestone 14 only after prompt 10 is complete and prompts 11–13 are complete. Read the revised specification, prompts 04, 06, 09, 11–13, the integration assessment, and current milestone reports. Inspect the existing ACP executor, task/lease system, model gateway, MCP registry, and Harness² adapter before changing scope. Preserve unrelated work. Do not assume any protocol or remote agent is supported until its exact version and capabilities are verified.

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

Write `docs/integrations/agent-delegation.md`, update architecture, compatibility and API contracts as needed, and write `docs/milestones/14.md` with exact commands/results, protocol versions, live-versus-offline evidence, migration/rollback notes, and remaining setup. Archive this prompt only after all acceptance criteria are evidenced.

## References to verify against pinned versions

- [A2A protocol specification](https://github.com/a2aproject/A2A/blob/main/docs/specification.md) describes Agent Cards, tasks/messages/artifacts, capability validation, and task update mechanisms. Pin a released revision and verify its actual authentication and operation support.
- [ACP overview](https://agentclientprotocol.com/protocol/overview) and [ACP extensibility](https://agentclientprotocol.com/protocol/v1/extensibility) define the existing coding-agent client/session boundary and negotiated extensions.
- [LiteLLM Agent Gateway](https://docs.litellm.ai/docs/a2a) may provide A2A routing/logging. Evaluate it as an optional adapter; its task/usage records do not replace Harnessa's records or policy.
- [AG-UI overview](https://docs.ag-ui.com/introduction) describes the agent-to-user boundary. Keep browser updates a projection of Harnessa's durable state.
