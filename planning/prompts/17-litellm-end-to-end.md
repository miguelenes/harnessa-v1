# Prompt 17 — End-to-end LiteLLM integration and optimization

Implement milestone 17 only after milestones 1–16 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, the planning index, and all prior milestone reports. Inspect the current gateway, versions, and worktree; preserve unrelated changes. Complete this milestone and stop.

At the time this prompt was drafted, `infra/compose.yaml` in the working tree pinned LiteLLM `v1.83.3-stable`; verify the current pin when implementing. Some currently documented capabilities, including Auto Routing, require newer releases and are beta. Do not configure a feature just because it appears in current documentation: confirm its availability, interface, migration impact, and behavior on the exact selected release.

## Prerequisites and owned scope

Milestone 13 release acceptance and all preceding milestone reports are complete. Extend the named subsystem through its existing ports, budgets and evidence records; use `planning/architecture-decisions.md` for shared failure/recovery and authority rules. Provider/agent access and spending remain explicitly configured, with offline fixtures for unavailable services.

## Objective

Deliver a secure, operable, cost-aware LiteLLM integration covering every LiteLLM-supported feature relevant to Harnessa's actual workloads. Establish a version-pinned compatibility and applicability matrix across providers, model/API endpoints, authentication, access control, budgets, routing/reliability, catalog/pricing, caching, prompt caching, Qdrant, context compression, guardrails, evaluations, logging/metrics, MCP, and A2A. Mark each capability `enabled`, `opt-in`, `deferred`, or `unsupported`, with exact version, rationale, configuration source, evidence, and owner. “Full integration” means every relevant capability is deliberately covered and connected end to end; it does not mean enabling irrelevant endpoints/providers, beta features, or unsafe cache/guardrail defaults.

## Existing platform integration

Reuse milestone 7’s Qdrant service and governed retrieval adapter, milestone 11 evidence, milestone 12/13 evaluation infrastructure, milestone 14 evolution gates, milestone 15 delegation and milestone 16 decision adapter. This milestone completes gateway configuration and measured optimization across those systems; it does not recreate them. Baseline feature support must be rechecked against the actual image at execution time.

## Implement

### 1. Versioned gateway and configuration ownership

1. Audit LiteLLM's current pinned container and SDK, database schema, Redis/cache services, Harnessa gateway adapter and request ledger, configuration files, health checks, deployment/secret references, supported client endpoints, and current provider inventory. Record a baseline before modifying anything.
2. Compare the deployed version against the exact-version documentation and supported feature set required below. Make and justify a compatibility decision: retain the pin and defer unavailable features, or perform a controlled upgrade with release notes, migration, offline compatibility coverage, staged rollout, and tested rollback. Pin image and dependencies immutably; do not use `latest` or a moving branch.
3. Establish one source of truth per gateway resource. Explicitly define how declarative config and LiteLLM database/admin state are reconciled, how drift is detected, and which changes require a Harnessa change record. Never silently let startup config overwrite user-managed models, keys, or teams.
4. Keep provider credentials and LiteLLM master/admin credentials inside the gateway's secret boundary, passed by supported secret references. Keep app/worker settings credential-free and prevent secrets from entering generated config, API responses, logs, traces, eval artifacts, and model inventory exports. Define separate least-privilege identities for gateway administration, Harnessa service calls, and end-user/team/project model access.
5. Add configuration validation and an operational readiness check that verifies database, cache, configured deployments, auth, spend logging, and endpoint compatibility. Readiness must not report success merely because the HTTP process responds. Provide a credential-free doctor path and an explicitly invoked live probe path with a caller-authorized cost limit.

### 2. Refreshed model and “free to use” inventory

1. Build a repeatable, timestamped catalog refresh from LiteLLM's upstream model-cost/capability map and appropriate provider-maintained catalogs/pricing/terms. Preserve source URLs, retrieval time, source revision/hash, LiteLLM map revision, and normalization decisions. Do not hand-maintain an untraceable allowlist or claim a permanent exhaustive list.
2. Retrieve all catalog entries with publicly documented zero input/output cost, plus separately identify published free-tier/trial offerings and locally hosted/open-weight models with no provider API charge. Include supported LiteLLM providers/models outside the zero-price subset in the full catalog so Harnessa can reason about viable alternatives and price changes.
3. Do not equate a zero price-map entry with usable account quota. Track these independently:
   - `public_zero_price`: current public source lists the applicable request/operation at zero price;
   - `account_quota_verified`: this configured account has verified access/quota and its limits/reset/region, or `unknown` when not checked;
   - `local_no_provider_fee`: model runs on an operator-managed endpoint, with infrastructure/energy/operations cost reported separately.
   Track promotional credits, free-tier caps, rate limits, waitlists, regional limits, and trial expiry as conditional entitlements, never as an unlimited free model.
4. Per model/deployment, record provider/model identity, alias, endpoint, supported API surfaces (chat, Responses, embeddings, reranking, image/audio or other modalities only as applicable), context/input/output limits, tools/structured-output/streaming support, pricing dimensions, rate/parallelism constraints, data retention/training terms where known, license/use restrictions, region, credential/access status, source provenance, refresh age, and compatibility probe evidence. Preserve `unknown` instead of inferring.
5. Implement eligibility and staleness rules. A route cannot be advertised as free/available unless its current pricing/access state, endpoint capability, workload compatibility, policy, and cost ceiling pass. Unknown pricing or account entitlement is excluded from a “verified free now” view. Expose catalog freshness and source evidence to admins, and retain historical revisions so spend estimates can be reproduced.
6. Do not run calls against user/provider accounts during catalog refresh. Any account-access or live capability probe must be explicit, bounded by the configured authorized spend limit, and recorded in the model request ledger. When credentials are absent, complete offline catalog ingestion and label account access as unverified.

### 3. Routes, keys, policies, and total spend

1. Extend prompt 3's interactive/primary and probe/batch pools with explicit compatibility predicates and reviewed model aliases. Route by workload needs (API semantics, tools, streaming, context, modality, latency, privacy/region, availability, quality evidence, and cost), not price alone. Never silently fall through to a model that lacks a required capability.
2. Define per-project/team/workload allowlists, virtual-key scope, model permissions, RPM/TPM/concurrency limits, budgets, daily/monthly spend ceilings, provider-specific limits, maximum request/output tokens, and alert thresholds. Keep Harnessa's project/repository authorization and atomic task/run reservation authoritative; reconcile LiteLLM's per-request spend without double counting. Missing provider usage remains unknown, not zero.
3. Assign one retry/fallback owner per request path. Bound attempts, delay, timeout, fallback count, and total token/cost envelope across nested client, Harnessa, and LiteLLM retries. Preserve the original request/trace and account for classifier, embedding, decision, compaction, retry, A2A, and cache costs. Prevent fallback loops and tools/workflows from being repeated after a partial stream or side effect.
4. Evaluate LiteLLM Auto Router or adaptive routing only if supported by the selected pin. Treat beta features as disabled by default and feature-flagged; use shadow mode first, disclose classifier cost/latency, log routing cause and model/policy revision, and allow routing only among deployments already approved by Harnessa. Compare against the fixed-route baseline. Keep prompt 16's Jev decision semantics and Harnessa thresholds authoritative; do not duplicate decision logic inside opaque gateway defaults.
5. Define cost estimation and reconciliation for every supported endpoint and billing dimension. Include provider discounts/price overrides only when their provenance and expiry are recorded. Update stale/missing cost metadata safely and fail closed for live work if the worst-case cost cannot fit the remaining reservation.

### 4. Qdrant, retrieval, and caching

1. Reuse prompt 7's Harnessa retrieval/index service and Qdrant deployment if present. Document separate collections/namespaces, credentials, retention, backup, deletion, and scaling needs for permission-filtered RAG vectors and any LiteLLM semantic response cache. Do not duplicate source ingestion or treat semantic-cache responses as RAG evidence.
2. Make deterministic exact-request caching the baseline where the request is safe to reuse. For shared Redis/Valkey, configure authenticated/TLS access as applicable, namespace/version isolation, TTL and invalidation, availability/failure behavior, and multi-worker correctness. Do not use per-process in-memory cache as a shared production cache. Measure hits and cost/latency saved against cache storage/operations costs.
3. Evaluate LiteLLM-supported Qdrant semantic cache as an opt-in optimization, not a default for agent traffic. Define key dimensions and isolation for tenant/project, model/deployment, endpoint, system/prompt/policy revision, tools/schema, retrieval snapshot, locale, and request metadata. Require permission checks before cache lookup and return. Exclude side-effecting operations and stateful multi-turn/tool traffic unless a separate evaluation demonstrates safe equivalence. Set similarity thresholds, TTL, invalidation, Qdrant outage behavior, and bypass controls.
4. Evaluate false-hit rates, stale-policy reuse, cross-tenant leakage, semantic mismatch, exact-cache behavior, hit-rate/savings, and cache outages on a frozen, risk-stratified suite. A cache hit must remain attributable to the original request class and cache policy revision. Add admin-visible bypass/flush/disable controls and auditable cache metrics without exposing prompt content.
5. Distinguish gateway response caching, Qdrant semantic caching, provider-native prompt/prefix caching, and Harnessa retrieval/vector storage in code, config, docs, and spend reports. They have different correctness and cost semantics.

### 5. Compression and context efficiency

1. Inventory LiteLLM-supported prompt/context compression, compaction, and pre-call hooks for the selected release, including TypeSafe Jev compaction when its guardrail is present. Verify the actual provider/API compatibility and data flow. Do not deploy an unpinned plugin or external compressor without an explicit privacy, licensing, and version review.
2. Make compression opt-in by workflow/team/model route, with an explicit policy version, token/latency/cost limits, and bypass. Record whether compression ran, what class of content was removed or summarized, compressor/model version, and input/output token counts; avoid storing raw sensitive content by default.
3. Preserve system and current user instructions, required evidence/citations, current task state, code/patch context, tool call/result pairing, structured-output constraints, and any content required to safely continue. Use conservative failure behavior: on timeout, malformed judgment, or unsupported API, bypass or fail the request according to the configured workload policy; never silently drop needed context.
4. Evaluate answer/task quality, tool correctness, citation integrity, regressions by task/risk type, context reduction, latency, and all-in cost versus the uncompacted baseline. Include the cost of Jev/classifier/compression calls. Do not promote compaction based solely on token savings. Keep this distinct from provider prefix caching and exact/semantic response caching.

### 6. Guardrails and request hooks

1. Inventory LiteLLM-supported pre-call, during-call, and post-call guardrails, request/response transformations, and callbacks/hooks available in the selected build. Record data sent to each external guardrail, provider, retention terms, failure semantics, API compatibility, and incremental cost/latency.
2. Map eligible guardrails to Harnessa's existing policy and data-handling requirements (for example, secret/PII redaction, content safety, or prompt-injection detection where the workload calls for them). Use deterministic Harnessa authorization and validation for permissions and effects; a model guardrail score or allow decision cannot grant access, approve a repository mutation, or replace task acceptance.
3. Define fail-open/fail-closed behavior per workload and risk class, timeout and retry bounds, transformations/redaction provenance, human escalation, and safe behavior when a guardrail is unavailable. Ensure hooks cannot remove system policy, required citations, task state, or tool constraints. Do not silently transform requests.
4. Evaluate false allow/false block rates, evasion, privacy leaks, regressions in code/tool/structured responses, partial-stream behavior, and complete added cost/latency on a risk-stratified suite. Keep any external or beta guardrail opt-in until those acceptance thresholds pass.

### 7. Evaluations, shadowing, and optimization loop

1. Reuse Harnessa's prompt 13 evaluation center, prompt 11 provenance, prompt 14 promotion gates, and prompt 16 Jev evaluation contracts. LiteLLM benchmark or shadow-evaluation features may supply traffic/model observations but must not become a second source of acceptance or promotion policy.
2. Build a versioned, permission-reviewed, redacted evaluation suite with representative workloads, task outcomes, risk strata, provider/API requirements, tool-call traces where safe, retrieval revisions, and independently labeled expected results. Keep hidden/holdout data inaccessible to tuning, classifiers, candidate prompts, and model authors.
3. Compare a reproducible fixed-route baseline against candidate free/paid routes, Auto Router/Jev choices, retries/fallbacks, exact/semantic/prompt cache policies, compression, and any relevant guardrails. Use paired inputs/seeds where applicable; report sample size and uncertainty. Report success/quality by risk tier, accepted outcome, errors, tool correctness, retrieval/citation correctness, route distribution, fallback rate, p50/p95 latency, tokens, cache behavior, and total cost per accepted outcome.
4. Count all spend and resource costs: primary calls, Jev/router/classifier calls, embeddings/reranking, compressed or retried requests, failed attempts, A2A agent calls, cache/Qdrant infrastructure, and any eval/judge calls. Reconcile reservations with provider bills/logs. Configure a hard aggregate evaluation budget using the existing authorized project policy; stop before exceeding it and do not invent or increase the approved cap.
5. Roll out optimizations in shadow mode, then a bounded canary with owner, rollback trigger, and fixed evaluation window. Promotion requires a measurable benefit within the approved risk/quality thresholds and no protected-stratum regression. Retain baseline, candidate config hash, exact model/deployment versions, source/eval revisions, and rollback record.

### 8. A2A, MCP, and gateway observability

1. Extend prompt 15's Harnessa-owned delegation lifecycle through LiteLLM's Agent Gateway only for remote agents selected by policy. Pin and test A2A protocol versions (including 0.3/1.0 where supported by the selected build), agent identity/card revision, auth headers, key/team/agent access, streaming/status semantics, cost tracking, kill-switch/iteration limits where supported, and failure reconciliation. Harnessa child task and budget records remain authoritative; remote agent status/artifacts are external evidence until validated.
2. Reuse prompt 7's MCP registry/grants when evaluating LiteLLM's MCP Gateway. Do not let a LiteLLM team/key permission widen Harnessa project/tool grants. Mutation tools must continue through the Harnessa command/effect-intent boundary, with invocation IDs and scoped credentials.
3. Correlate every LLM, decision, embedding, cache, compression, MCP, and A2A operation using Harnessa task/run/attempt IDs plus LiteLLM request/agent IDs and stable trace context. Emit route/deployment/revision, attempt and fallback cause, latency, token/use/cost status, cache/guardrail outcome, and redaction labels. Never log raw prompt/response by default; if content capture is separately enabled, require narrow retention and access rules.
4. Integrate the supported callbacks/logging/metrics path with the existing observability system. Add dashboards/alerts for gateway readiness, provider availability, 429/5xx/timeouts, stale pricing/catalog, unknown spend, budget denials, retry amplification, cache hit/false-hit evidence, Qdrant health, A2A/MCP failures, and cost/quality drift.
5. Test reconciliation after restart, duplicate callbacks/events, partial streams, missing usage fields, database/cache outage, config drift, credential rotation, and provider/catalog changes. An admin control to disable a provider, route, beta feature, cache, compression, MCP server, or A2A agent must take effect predictably and be auditable.

## Acceptance

- `docs/integrations/litellm.md` contains the exact pinned version, config/data-flow diagram, responsibility boundaries, provider/API compatibility matrix, enabled/opt-in/deferred/unsupported feature matrix, secrets and identity model, route/retry/budget policy, current model-catalog procedure, operations/readiness/upgrade/rollback runbook, and links to source evidence.
- A generated catalog snapshot demonstrates complete ingestion of all eligible zero-public-price entries from the selected LiteLLM catalog revision, separately identifies free-tier and local models, preserves provenance and refresh time, and clearly distinguishes verified account quota from unknown access. No model is marked verified free/available from price metadata alone.
- For every advertised model route, compatibility, account state, pricing status, cost bound, policy eligibility, and failure behavior are explicit. Unsupported or unknown capability is rejected or shown as unknown rather than guessed.
- Harnessa and LiteLLM budgets reconcile without duplicate charging; nested retries and fallback are bounded; missing usage remains unknown; interactive capacity and project/task ceilings remain enforced.
- Qdrant RAG data and response-cache entries are isolated and governed independently. Cache authorization, invalidation, bypass, failure behavior, and false-hit evaluation are evidenced. Agentic semantic caching remains opt-in unless its workload-specific acceptance evidence passes.
- Compression is opt-in and versioned, preserves required state, has safe bypass/error handling, and demonstrates quality and all-in cost against an uncompacted baseline before any default enablement.
- Evaluations produce a reproducible fixed baseline and candidate comparison with risk-stratified quality, latency, reliability, and all-in cost; aggregate savings cannot hide protected-stratum regressions. The authorized evaluation spend cap is enforced.
- Remote A2A/MCP gateway operations preserve Harnessa scope, budget, traceability, task lifecycle, and artifact verification. Any unconfigured/live external capability is explicitly marked pending, with no fabricated live evidence.
- Readiness, observability, secret redaction, graceful degradation, upgrade/rollback, and provider/cache/config change handling are documented and verified using deterministic offline fixtures. Any live credentialed check is opt-in and bounded.
- Update model/provider compatibility docs and operational docs, add migrations only when needed, and write `docs/milestones/17.md` with exact versions, catalog sources/revision/time, evaluation dataset and cost policy, commands/results, live-versus-offline evidence, limitations, outstanding setup, and the next prerequisites. Keep this prompt active unless every acceptance criterion is evidenced.

## References to verify against the pinned build

- [LiteLLM gateway and access/spend controls](https://docs.litellm.ai/docs/simple_proxy)
- [LiteLLM model cost map and model catalog](https://docs.litellm.ai/docs/proxy/model_cost_map)
- [LiteLLM cache types, including Qdrant semantic cache](https://docs.litellm.ai/docs/proxy/caching)
- [LiteLLM Auto Routing beta and supported strategies](https://docs.litellm.ai/docs/proxy/auto_routing)
- [LiteLLM Agent Gateway/A2A](https://docs.litellm.ai/docs/a2a)
- [LiteLLM observe/evaluate recipes](https://docs.litellm.ai/docs/learn/observe-and-evaluate)
- [LiteLLM and TypeSafe Jev context compaction](https://docs.litellm.ai/blog/typesafe-jev-compaction)

Upstream documentation is a discovery aid, not proof of support in the pinned runtime. Confirm every configuration key, endpoint, protocol version, provider capability, pricing field, and behavior against the exact release and record the evidence.

## Delivery contract

Own only the subsystem named by this prompt and its existing adapters/UI projections. Preserve the shared authority, contract-generation, cost, recovery, and retention rules in `planning/architecture-decisions.md`. Produce the implementation, focused acceptance evidence, updated compatibility/setup notes, and `docs/milestones/17.md`. Record offline versus live results separately, exact versions/configuration and source revisions, pending external setup, recovery/rollback commands, and the next milestone’s prerequisites. Missing required evidence keeps the milestone active.

During this run, put all milestone deliverables—including implementation, documentation, migrations, and `docs/milestones/17.md`—in one PR. Obtain an independent code review, resolve findings, and merge the reviewed head in this run after required checks and repository protections pass. Do not self-approve or bypass protections. Include the PR URL, reviewed head SHA, review outcome, check results, and merge commit SHA in the final run summary. A blocked review or merge leaves milestone 17 incomplete and active; do not archive its prompt.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced; the future archive target is `planning/archive/completed/17-litellm-end-to-end.md`. Never run the next milestone as part of this request.
