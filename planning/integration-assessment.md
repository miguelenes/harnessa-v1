# Integration assessment: model, tool, agent, and user protocols

This assessment extends the initial ten Harnessa milestones. It does not claim those milestones are implemented or verified; current local evidence covers only milestones 1 and 2. Recheck upstream versions and interfaces when implementing because provider products and protocols change.

## The shared integration point

These systems do not form one interchangeable orchestration protocol. They meet at the boundaries around a Harnessa task/run:

1. Harnessa owns task state, authorization, budgets, durable events, leases, effect intents, and final acceptance.
2. LiteLLM handles model request routing and gateway-level request/spend telemetry. Each task/run can contain many model requests, retries, and tool calls.
3. Harness² runs propose/probe/compose experiments and emits candidate harnesses, executions, and benchmark results. Those are imported, immutable evidence and do not themselves authorize promotion.
4. GitHub webhooks report external repository events. Their delivery IDs and signatures support ingestion and deduplication, but each payload must be reconciled against current repository/PR/head state before it affects Harnessa.
5. AG-UI projects durable run activity to the browser. It supports interactive streaming but is neither a durable event store nor an authority boundary.
6. MCP and skills shape tool/context access. MCP is an interoperability layer; a gateway permission does not replace Harnessa project/repository authorization. Skill and retrieved content are untrusted inputs and cannot change policy.
7. ACP manages a client-to-agent session, especially for a local coding agent. A2A describes task delegation between independently hosted agents. Neither replaces Harnessa's durable scheduler, parent/child task records, or approval policy.
8. TypeSafe Jev is a structured-decision model: typed questions produce values/probabilities that application code can compose. Its best fit is bounded decisions inside a known workflow, not open-ended planning or code generation.

The common foundation is a **versioned evidence and provenance graph** with stable Harnessa IDs, source-system identifiers, correlation/causation links, revision hashes, explicit trust and redaction labels, and idempotent import/reconciliation. Persist domain transitions and intent before external side effects. Preserve source-specific authority rather than flattening everything into a generic event or score.

## Integration matrix

| Area | Source of truth | Useful join keys | Harnessa responsibility | Main failure to prevent |
| --- | --- | --- | --- | --- |
| Task/workflow | Harnessa PostgreSQL | task, run, attempt, aggregate sequence, base SHA | Authorization, leases, budgets, terminal outcome | External event overwrites task state |
| Model use | LiteLLM gateway plus Harnessa reservations | gateway request ID, request tags/metadata, route/deployment, task/run IDs | Reserve ceiling, correlate each attempt, reconcile billed/unknown usage | Double counting retries or treating missing usage as zero |
| Harness² | Pinned Harness² run artifacts | source commit, manifest, benchmark/task, executor, harness digest | Import checksummed outputs; evaluate/promote under Harnessa policy | Candidate/editor accesses hidden graders or claims unsupported gain |
| GitHub | GitHub repository/PR/check state | installation/repo ID, delivery UUID, PR/check ID, head SHA | Verify signature, persist before ack, dedupe, authorize answers/effects, reconcile | Replay, stale head, echo loop, duplicate write |
| Browser stream | Harnessa durable events projected as AG-UI | task/run IDs, event cursor/sequence, protocol event ID | Replay/retention/auth and mapping; mutations remain HTTP/domain commands | Browser stream becomes sole record or authority |
| MCP | Harnessa grants plus upstream server capability | server alias, tool name, protocol version, invocation ID | Discover/allowlist, scope, audit, bound output, revoke | Gateway/tool result expands authority |
| Retrieval/skills | Versioned source commit/artifact and skill digest | repository/revision/path/hash, skill revision, evidence links | Filter by grants, cite provenance, expire/revalidate, isolate caches | Cross-project retrieval or prompt injection promoted as policy |
| Agent delegation | Harnessa delegation record; remote A2A task is an external execution record | parent task/run, child delegation, agent identity/card revision, A2A task ID, artifact hash | Persist before dispatch, scope grants/budgets/deadline, reconcile status/artifacts, cancel orphans | Agent escapes parent authority, unbounded fan-out/loops, duplicate child work |
| Structured decisions | Harnessa's versioned workflow/question definition plus provider response record | decision-run ID, workflow/question-set revision, typed outputs, actual Jev version, input-state digest | Validate schema, combine outputs in deterministic code, log thresholds/cost/latency, escalate uncertainty | Treating model confidence as calibrated for Harnessa data or as authorization |

## Assessment by candidate system

### LiteLLM

**High-value integration:** propagate stable task/run/correlation tags into every model request, ingest gateway request IDs and route/fallback/usage outcomes, and reconcile gateway spend with Harnessa's pre-dispatch reservations. Gateway cost logs are per-request/attempt, while Harnessa budgets span all attempts and concurrent probes. Keep prompt/response storage disabled by default and use metadata references instead of duplicating sensitive content.

**MCP opportunity:** LiteLLM's MCP Gateway can centralize server access and key/team-level controls. Treat that as an additional access layer, not a replacement for project grants or domain authorization. For mutation tools, have the server call Harnessa's existing command/effect-intent service.

**End-to-end optimization:** prompt 16 audits the exact pinned LiteLLM build and configures the supported capabilities that fit Harnessa's workloads. It joins model discovery and zero-price eligibility, provider credentials, task routes and spend ceilings, cache policies, Qdrant retrieval, compression/guardrails, evaluations, observability, and the Agent Gateway into one evidence-backed operating model. It owns a versioned feature applicability matrix (enabled, opt-in, deferred, or unsupported for the pinned build) and a repeatable refresh/reconciliation process. LiteLLM's model price map is useful for discovery, but a listed $0 rate does not prove the user's account has quota, that access is enabled, or that provider terms permit the workload. Therefore, track public zero-price metadata, verified account quota, and local models with no provider fee as distinct states.

**Qdrant and cache boundary:** Harnessa RAG collections hold permission-filtered source chunks with revision and citation provenance. LiteLLM semantic-cache entries are response-reuse records with their own namespace, metadata, and invalidation policy. Do not treat a cache hit as retrieval evidence, share cache entries across projects/teams by default, or apply semantic response reuse to stateful agent/tool traffic without a passing false-hit evaluation. Evaluate deterministic exact-request cache policies where safe; provider prompt-prefix caching is a separate capability.

### Harness²

**High-value integration:** pin upstream revision and supported executor, use the adapter, import complete run manifests and candidate/result artifacts, and connect each result to the exact model route, skill/harness digest, evaluation set, seed, environment, and cost. Harness²'s task-level adaptation and persistent harnessing-level learning fit Harnessa's existing candidate/evaluation/promotion concept, but the output needs Harnessa's policy gate and isolated holdout.

**Limits:** upstream setup and benchmark dependencies are material; reported benchmark gains do not establish Harnessa performance. Hidden evaluation artifacts must remain outside candidate editors, retrieval, probes, and composers. Keep public development feedback separate from private grader evidence.

### GitHub webhooks

**High-value integration:** use delivery UUID as an idempotency key, validate the raw-body SHA-256 HMAC, persist accepted delivery and enqueue work before a fast 2xx response, and reconcile missed/reordered/stale events against live PR/check state. Bind check completion to exact repository and current head SHA. Keep webhook intake separate from execution workers and outbound write credentials.

**Limits:** valid signature proves payload origin/integrity relative to the shared secret; it does not prove the payload is current, authorized to answer a question, or safe to cause a mutation. Delivery redelivery uses the original delivery ID and must not duplicate effects.

### Web AG-UI

**High-value integration:** translate durable run/task events into typed lifecycle, message, tool, activity/state, and domain-specific events. Include Harnessa IDs and replay cursor metadata. Use existing authenticated HTTP commands for answers and state changes, and use AG-UI for interactive updates/reconnectable projections.

**Limits:** the protocol does not supply durable replay storage, authorization policy, a workflow engine, or exactly-once effects. Never stream private reasoning; expose concise status and tool activity with redaction.

### Agent-to-agent communication

**Protocol roles:** use ACP for local client↔coding-agent sessions and negotiated capabilities; use A2A only when delegating to a separately hosted agent with a discoverable, authenticated endpoint. Use MCP for an agent's tools/data and AG-UI for browser interaction. Internal Harnessa workers should continue to use durable domain dispatch rather than wrapping every worker as an A2A service. LiteLLM's A2A gateway can be evaluated as an optional remote-agent ingress/routing/telemetry layer, not as the parent-task authority.

**Delegation contract:** persist a Harnessa child delegation record and reserve a share of the parent budget before dispatch. Bind it to a selected agent identity and Agent Card/capability revision, allowed task type, input artifact hashes, repository/project grant, maximum cost/tokens/time, concurrency/fan-out limit, deadline, and cancellation policy. Forward only scoped credentials; an agent's own identity or capabilities cannot widen the parent's grants. Import remote task IDs, status, and content-addressed artifacts as external evidence; accept a result only after schema, policy, provenance, and acceptance checks.

**Limits:** remote agents are opaque and may be nondeterministic. A2A task status is not a Harnessa task transition, a returned artifact is not verified output, and a remote webhook/stream is not authorization. Treat timeouts as unknown until reconciled, use stable idempotency keys where the remote agent supports them, and avoid retrying a child when its previous submission may have succeeded. Cap delegation depth, fan-out, total budget, wall time, and queued child tasks to prevent loops and cost amplification.

### TypeSafe System One / Jev

**High-value integration:** represent decisions as versioned workflow code plus narrow typed questions (Choice, Score, Noul). Batch independent questions against one bounded state input; combine scores, probabilities, hard rules, cost constraints, and safety gates in ordinary deterministic code. The decision graph can classify task type/complexity/tool needs, estimate review or escalation priority, select among already-authorized model tiers, identify likely clarification needs, and select candidate Harness² evaluation strategies. These can replace repeated free-form model calls for specific decision nodes when measured workflow quality holds.

**Model routing:** have Jev estimate task properties or distribute probability across an allowed set of route classes. The application chooses only from deployments already admitted by compatibility, policy, budget, availability, and context checks. Use expected quality/risk under a cost ceiling to choose a route, not the label `simple` alone. When uncertainty is high, risk is high, the request is out of distribution, or Jev is unavailable, fall back to the established conservative route or human review. A typed response reduces parsing/schema failure; it does not make the semantic decision correct.

**Cost control:** compare total spend including Jev calls against the fixed-route baseline. Jev is worthwhile only when its cost and latency are outweighed by avoided expensive-model calls, retries, unnecessary probes, or human review without unacceptable quality loss. Cache a decision only against a content-addressed input state and workflow version, with a short explicit TTL where freshness matters; never reuse a decision across changed task/repository/policy state.

**Limits and evaluation:** TypeSafe describes Jev as early access and reports its own workflow comparisons against reference-model consensus, while noting possible workflow-author bias. Treat pricing, latency, confidence calibration, and accuracy as vendor claims until verified on Harnessa's tasks. Build a separately adjudicated gold set; compare Jev, deterministic rules, fixed-tier LLM, and Jev-plus-LLM cascade on the same frozen workflow. Measure task-level quality, selective coverage/abstention, escalation rate, calibration (for example Brier score/reliability bins), p50/p95 latency, retries, and all-in cost. Segment by task type and risk; do not promote routing on aggregate savings if high-risk strata regress.

## Recommended prompt order

1. **Prompt 11 — Evidence and provenance.** Establish source-specific adapters, join keys, replay/reconciliation, result lineage, and UI inspection. This gives RAG and learning a trustworthy substrate.
2. **Prompt 12 — Governed retrieval and MCP.** Index only authorized, versioned sources; provide citations and manifests; scope tool access and bind mutations to existing authority. Lexical search should work without an embedding vendor.
3. **Prompt 13 — Skill and harness evolution.** Derive candidates from verified evidence; compare against matched baselines in isolated environments; promote only under configured policy; retain rollback.
4. **Prompt 14 — Governed agent delegation.** Extend local ACP capability records and add optional remote A2A delegation only where independent agent interoperability is useful; retain Harnessa-owned parent/child lifecycle, budgets, permissions, and evidence validation.
5. **Prompt 15 — Structured Jev decisions.** Add versioned typed decision workflows and an evaluated cascade/routing layer after route and provenance contracts exist; make all thresholds and fallback policy application-owned.
6. **Prompt 16 — End-to-end LiteLLM operations and optimization.** Complete the pinned gateway feature audit and integrate refreshed model eligibility, provider setup, Qdrant, cache and compression experiments, route selection, A2A, evaluations, spend reconciliation, and operational controls over the foundations established above. Measure all-in quality, latency, reliability, and cost before enabling optimizations by default.

Do not start these prompts until milestone 10 has integrated the initial views and runtime contracts. If an earlier milestone creates equivalent features, reconcile the prompt scope and acceptance evidence instead of duplicating subsystems.

Prompt 16 is not a request to turn on every LiteLLM feature. Provider APIs, add-ons, and beta features change independently; record applicability against the selected LiteLLM release and test exact configuration behavior before exposure. Keep Harnessa as the authority for project/repository grants, durable task lifecycle, parent/child delegation, overall budget reservation, acceptance, and promotion. LiteLLM remains the model/tool/agent gateway and per-request telemetry layer.

## Current source references

- [LiteLLM AI Gateway](https://docs.litellm.ai/docs/simple_proxy) documents routing, fallbacks, spend tracking, and logs; [LiteLLM MCP Gateway](https://docs.litellm.ai/docs/mcp/) documents key/team/server access controls.
- [Harness² upstream README](https://github.com/google-research/harness2) documents its editable components and propose/probe/compose workflow. Its [setup guide](https://github.com/google-research/harness2/blob/main/docs/SETUP.md) lists benchmark dependencies and credential expectations.
- [GitHub webhook best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks) and [signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries) document delivery IDs, fast acknowledgement, HMAC, and safe intake practices.
- [AG-UI overview](https://docs.ag-ui.com/introduction) defines it as an event-based agent/user interaction protocol; the [Python event reference](https://docs.ag-ui.com/sdk/python/core/events) documents event families and fields.
- [A2A protocol specification](https://github.com/a2aproject/A2A/blob/main/docs/specification.md) defines Agent Cards, task/message/artifact models, capabilities, and polling/streaming/push delivery; [LiteLLM Agent Gateway](https://docs.litellm.ai/docs/a2a) is an optional routing/logging integration to evaluate.
- [ACP overview](https://agentclientprotocol.com/protocol/overview) describes client↔agent session communication. Keep it distinct from agent↔agent task delegation.
- [TypeSafe introduction](https://docs.typesafe.ai/introduction) documents Choice, Score, and Noul primitives and recommends decomposing compound judgments into narrow questions combined in code. Its [Jev announcement](https://typesafe.ai/blog/introducing-system-one-models-and-jev) describes the product's current early-access status and caveats around published comparisons.
- [TypeSafe workflow evaluations](https://evals.typesafe.ai/) illustrate fixed workflows that combine deterministic rules and narrow model decisions. Treat vendor-published comparison labels as reference evidence, not an independent Harnessa gold set.
- [MCP introduction](https://modelcontextprotocol.io/introduction) describes the tools/data interoperability boundary.
