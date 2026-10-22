# Harnessa — revised implementation specification

## 1. Product and scope

Build a self-hosted control plane that turns a repository topic into a specification, runs an isolated coding agent, verifies the resulting change, coordinates GitHub review, and learns evidence-supported improvements to future agent harnesses. Deliver a real full-stack product, not the simulated UI requested in the source document's code-generation appendix.

Initial deployment: Linux, one organization per installation, multiple users and explicitly connected repositories. Roles: administrator, operator, reviewer, viewer. Enforce project access in the API, event streams, worker commands, artifact storage, and GitHub identity mapping. Public multi-tenant SaaS and Kubernetes are outside the initial scope.

Keep the original seven pillars, ten board columns, and six principal views. Bring up one real end-to-end agent/provider combination first; add the others through tested adapters. Missing credentials disable only the corresponding live integration and show a clear setup state.

## 2. Architecture and reuse

Use a modular monolith with separately runnable API and worker processes. Prefer Python 3.12/uv, FastAPI, Pydantic v2, async SQLAlchemy/Alembic, PostgreSQL 16, Redis Streams, a pinned OpenSpec Node runtime, and LiteLLM. Use Next.js/TypeScript, Tailwind, shadcn/ui, dnd-kit, Monaco, and Recharts for the frontend. Select maintained compatible frontend releases during milestone 1 instead of assuming the attachment's Next.js 15 pin is still appropriate. Lock package versions and container image digests.

PostgreSQL is the authority for domain state and durable events. Redis Streams transports work and can be rebuilt from retained database events. Start repository lookup with Git/filesystem indexes and lexical search. Pilot LlamaIndex ingestion/retrieval with Qdrant behind the same interface in milestone 7, measuring benefit and retaining lexical fallback. Apply Harnessa authorization before queries and source opening; isolate RAG collections from LiteLLM semantic caches. Qdrant is the initial vector backend; a second pgvector implementation is not required.

See [shared planning decisions](architecture-decisions.md) for the milestone 4 LangGraph compatibility/ownership gate, official ACP/AG-UI SDK boundaries and Pydantic/OpenAPI-generated contracts. Initial release acceptance is milestone 13; evolution in 14–17 follows it. Prompts 1–3 retain their historical scope.

### Product brand, UI system, and visual assets

Establish a coherent Harnessa identity before building production screens. Document the brand direction, audience, product personality, color roles (including semantic status colors), typography, spacing, radii, elevation, icon treatment, and light/dark behavior in a concise design-system source of truth. Make the system recognizable and professional for a self-hosted engineering control plane; use accessible contrast and do not communicate state through color alone. Do not invent unsupported product claims or let decorative branding obscure operational state.

Use the selected component framework consistently. Extend shadcn/ui through shared, project-owned components and design tokens; compose feature screens from those shared primitives instead of duplicating styles or introducing a second component framework. Define reusable patterns for navigation, forms, dialogs, tables, status badges, task cards, empty/loading/error states, and confirmations. Keep application behavior and authorization in product code rather than burying them in visual wrappers. Record any deliberate exception and its reason.

Create and maintain a brand asset set appropriate to the shipped interfaces: primary and compact logo/wordmark treatments, favicon/app icon, and any needed illustrations or product imagery. Prefer hand-authored or repository-native SVG for logos, icons, and simple UI graphics so they remain crisp, themeable, accessible, and reviewable. Use the image-generation capability for original raster illustrations, textures, or other imagery when such assets add real value; do not use generated raster art as a substitute for a functional icon set or a precise wordmark. Inspect generated outputs, preserve the source and final files in the repository, and record their intended use and prompt/provenance in the asset documentation. Avoid placeholder marks, stock imagery that conflicts with the brand, fake interface screenshots, and unlicensed third-party assets. All required assets must resolve in production builds and have suitable size/crop variants where needed.

Keep branding assets separate from app interaction components. Provide meaningful alternative text for informative imagery and empty alternative text for decoration; use CSS/vector tokens for simple backgrounds and shapes when a bitmap adds no value. Include reduced-motion behavior for animated assets and optimize raster files for delivery.

Use **AG-UI at the UI boundary**: Python SDK events/encoding on the server and the TypeScript SDK/client on the frontend. Project durable run events into lifecycle, message, tool, state snapshot/delta, and custom events. Use authenticated HTTP commands for answers and mutations, and SSE for server-to-browser updates. Add WebSockets only for a justified feature such as an interactive terminal. The AG-UI stream is a projection, not a source of authority. Reconnect, replay, retention, and authorization remain application responsibilities. This recommendation follows the [AG-UI overview](https://docs.ag-ui.com/introduction), [Python events](https://docs.ag-ui.com/sdk/python/core/events), and [HTTP client](https://docs.ag-ui.com/sdk/js/client/http-agent).

Use **ACP between orchestrator and coding agents**, through the official Python SDK behind the existing agent-session port. Negotiate protocol version/capabilities; support initialization, session creation, prompts, updates, cancellation, permission requests, and optional resume only when advertised. Native ACP, external ACP adapters, and provider-specific execution bridges must be distinguished in the registry. A CLI name alone does not establish ACP support. See the [ACP overview](https://agentclientprotocol.com/protocol/overview).

`session/ask_orchestrator` and `decision/log` in the attachment are product concepts, not standard ACP methods. Prefer a scoped orchestrator tool exposed through an agent-supported mechanism, such as MCP. If an ACP extension is needed, negotiate it and use an underscore-prefixed extension such as `_harnessa/ask_orchestrator`, with JSON-RPC request IDs. Keep custom metadata in permitted extension fields. See [ACP extensibility](https://agentclientprotocol.com/protocol/v1/extensibility).

## 3. State, events, and recovery

Preserve these board stages:

`BACKLOG → SPECIFYING → READY → IN_PROGRESS → VERIFYING → HUMAN_REVIEW → LEARNING → DONE`

`CLARIFYING` and `HARNESS_PROBE` are branches from execution, not mandatory sequential stages. Record the return stage. Verification can return to execution within a bounded repair budget. Explicitly model rejected changes, closed-unmerged PRs, cancellation, and retry exhaustion.

Maintain separate execution status (`QUEUED`, `RUNNING`, `WAITING_FOR_INPUT`, `PAUSED`, `FAILED`, `CANCELLED`, `SUCCEEDED`) and a blocked reason. A card can remain in its last meaningful column with an obvious failure badge. Learning failure after successful merge must be visible and must never restart implementation.

Every transition passes through one authorization/guard service and an expected version. Persist state change, audit record, and outbox event in one transaction. Events contain event/schema version, organization/project/task/run identifiers, aggregate sequence, actor, timestamps, correlation/causation IDs, and payload. Consumers use durable inbox deduplication; external effects use intent records, stable keys, and reconciliation after ambiguous timeouts. Promise at-least-once delivery with idempotent handling, not universal exactly-once execution.

Claim tasks with renewable leases and monotonically increasing fencing tokens. Expired workers cannot publish artifacts or advance state. Define worker heartbeats, recovery checkpoints, dead-letter handling, bounded backoff, cancellation, and project/global concurrency limits. Git writes use a per-repository integration lock plus expected base SHA; task-local workspaces may proceed concurrently.

## 4. Persistence and artifacts

Replace growing task JSON blobs with entities for organizations/users/memberships, repositories and GitHub installations, tasks, task transitions, specification revisions, questions/answers, agent profiles, runs/sessions/checkpoints, leases, events/outbox/inbox, effect intents, verifications, review bundles/PR links, model deployments/pools/requests/reservations, harness revisions/probes/experiences/promotions, and artifact metadata.

Scope uniqueness correctly: an OpenSpec slug is unique per repository, not across the installation. Keep append-only audit and request ledgers. Store large traces and files in a content-addressed local artifact store, with a storage interface for an optional S3-compatible backend. Persist checksums, size, retention, access scope, and redaction status. Git remains the authority for specification and harness file content; database records reference immutable revisions instead of maintaining a competing mutable copy.

## 5. OpenSpec and readiness

Use the pinned OpenSpec package and discovered schemas/templates. `/opsx:*` names are assistant workflows; they are not interchangeable with executable CLI commands. Use supported `openspec` commands for scaffolding, instructions, status, validation, and archive. Do not invent a `spec-driven-with-adr` schema without shipping and validating it. See the [CLI reference](https://github.com/Fission-AI/OpenSpec/blob/main/docs/cli.md).

Canonical locations are `openspec/specs/`, `openspec/changes/<slug>/`, and **`openspec/changes/archive/<date>-<slug>/`**, correcting the attachment's `openspec/archive/` path. The [OpenSpec workflow example](https://github.com/Fission-AI/OpenSpec#see-it-in-action) confirms this layout.

Keep the proposed readiness rubric (proposal .25, scenarios .30, design .25, tasks .20), but label the score an advisory heuristic, not a probability. READY requires score ≥ .90, no unresolved blocking questions, valid artifacts, requirement-to-verification mapping, settled scope/interfaces, available required execution capabilities, and any configured human sign-off. Log assumptions and nonblocking questions explicitly. Do not offer an unrestricted force-ready bypass of validity, authority, or safety checks.

Freeze readiness to specification commit/content hash, repository base SHA, policy revision, and an approval record where required. Invalidate readiness after relevant edits or base drift. Resolve questions with cited repository/specification evidence and policy checks; model confidence alone never authorizes an architectural scope change. Scope-changing clarifications create a new specification revision and re-enter readiness assessment.

## 6. Models and budgets

Default to two workload pools: primary/interactive and probe/batch. Model provider accounts and deployments separately so a pool can hold multiple accounts. Use capability-tested routes for each agent: Chat Completions, Responses, Anthropic-compatible messages, tool calls, streaming, context limits, and cancellation as applicable. One `OPENAI_API_BASE` variable does not configure every CLI.

Support bounded retries, Retry-After, cooldowns, circuit breakers, explicit compatible fallbacks, interactive capacity reservations, and optional probe overflow. No silent downgrade from a coding model to a fast model. Remove the unsubstantiated `<15ms` failover promise; measure route decision time and full recovery latency separately. LiteLLM already supplies [routing, retries, cooldown, and fallback building blocks](https://docs.litellm.ai/docs/routing); application policy and accounting wrap those features.

Reserve estimated maximum spend before dispatch, cap output/steps/time/concurrency, reconcile actual usage by request ID, and keep uncertainty visible when provider usage is missing. Concurrent probes share the parent task ceiling. Track estimated versus billed cost and currency/price provenance. A streaming failure cannot transparently restart already executed tools. Retrying a model request and retrying an agent run are separate operations.

## 7. Isolation and authority

Use rootless Podman containers by default, with a Docker adapter if needed. Each run uses a managed clone/workspace at an exact base commit. A worktree alone is not a security boundary; use a dedicated managed clone as its parent if worktrees are chosen. Agent-visible Git metadata must never expose another task's repository or host checkout.

Keep container orchestration in a narrow trusted runner/broker. Never mount a container engine socket or the operator's home/SSH/provider credentials into an agent. Enforce resource/time/process limits, scoped mounts, filesystem path checks, egress policy, and short-lived gateway credentials. Agent-authored commands and harness files cannot change installation policy, repository grants, evaluator fixtures, or promotion rules. Redact secrets before trace storage or streaming. Render untrusted Markdown without executable HTML.

Autonomous specification and coding continue within configured project grants. Publishing branches/PRs, merging, destructive commands, and harness promotion each have explicit project policy. Prompt injection in files, GitHub comments, tool outputs, or retrieved text cannot expand authority. Show concise decisions, citations, and tool activity; do not request or expose private chain-of-thought.

## 8. Verification, GitHub, and archive

Verification must independently execute approved commands in clean isolation and map every requirement/scenario to evidence or an explicit manual/unverified status. Checked `tasks.md` boxes and model summaries are insufficient. Bind results to code SHA, spec hash, harness revision, model route, image digest, and command manifest. Changes invalidate affected results.

Use a GitHub App with least privileges and explicit installation/repository opt-in. Persist verified webhook deliveries before acknowledging them, then process asynchronously. PostgreSQL deduplication is authoritative; Redis TTL caches are optional accelerators. Authorize comment authors and bind answers to question IDs/revisions. Correlate PR/check events with repository and current head SHA; do not rerun work because of an unrelated or stale check.

Persist outbound effect intents, identify app-authored content, suppress echo loops, and periodically reconcile missed/reordered webhook events. Human review must satisfy configured checks and branch protection; an approval button does not itself prove merge.

Default archive policy: prepare specification sync/archive in the implementation PR before final review, then finalize the corresponding records after verified merge. Preview the spec diff and rerun guards before publication. If a repository requires archive after merge, create a separate maintenance PR on the new base and track it through completion; never silently push updated canonical specs to the protected default branch. DONE requires verified integration and durable archive/cleanup outcomes. Optional learning can fail independently without reversing merged work, but its terminal outcome must be recorded.

## 9. Harness² and evaluation

Integrate a pinned upstream Harness² revision through an adapter instead of reimplementing the research algorithm. Its [README](https://github.com/google-research/harness2#harness-components) describes eight editable component categories and task-level contrastive propose/probe/compose without ground-truth feedback. Treat the attachment's exact filenames as a product manifest convention; verify actual upstream layouts. Its [setup guide](https://github.com/google-research/harness2/blob/main/docs/SETUP.md) documents executor-specific requirements, so compatibility needs a real adapter test.

Use immutable common-base execution harnesses, matched isolated candidate workspaces, bounded k=1..3, and both parallel/sequential modes. Execute the composed harness again. Keep independent holdout graders, expected outputs, scores, and hidden fixtures outside editor/probe/composer/reflection access. Normal visible development feedback is separate from held-out evaluation. Harness changes cannot weaken immutable security policy or alter their evaluator.

Persist evidence-supported cross-task lessons with source, scope, confidence label, expiry/revalidation, and conflict handling. Do not automatically turn every merged task into an active global harness. Create candidate versions, evaluate regression/cost/security constraints, then promote through an explicit configured policy with rollback. Track task-level adaptation and improvement-harness learning separately.

Support local repository acceptance suites first and adapter-based WorkBuddy-Bench, JobBench, and LAB integration with licensing/setup checks. Missing prerequisites mean blocked/unavailable, never an invented score. Show sample sizes, matched baselines, variance where meaningful, latency, cost, and failure rates.

## 10. Release definition

Ship all six views: board/task inspector, OpenSpec explorer, Harness Studio, evals, routing, and event inspector. Include onboarding for repository grants, agents, routes, budgets, and GitHub setup; keyboard-accessible controls; loading/empty/error/reconnect states; real read/write operations where authorized; and clearly separated development fault injection.

Ship the documented brand and reusable component system, complete logo/icon/app-icon assets, and any selected generated raster artwork with source/provenance and production build integration. Confirm all screens use shared components and tokens, and include responsive, accessible light/dark states.

Ship Compose deployment, secure bootstrap/login, migrations, backup/restore of database/Git/artifacts, secret-reference setup, health/readiness, structured redacted logs, metrics/traces, retention, cleanup, upgrade/rollback procedures, and a doctor command. Prove one complete live path in a disposable authorized repository. Keep deterministic offline acceptance available and label it separately from live external evidence. No claims of universal agent support, live production readiness, or learning gains without measured evidence.
