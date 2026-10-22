# Shared planning decisions and delivery contract

This is the planning contract for milestones 4–17. It records approved direction, not completed implementation. Read the current code and milestone evidence before applying it. Prompts 1–3 and archived reports are historical inputs and remain fixed. A draft report or report with missing required checks is not completion evidence; recheck milestone 3 at the handoff.

## Runtime ownership and the LangGraph decision

Harnessa PostgreSQL owns user-facing task state, authorization, budgets/reservations, effect intents, durable evidence and audit events. Redis is a rebuildable transport. Milestone 4 compares the current run scheduler against a pinned LangGraph candidate for run-level step orchestration, checkpoints and interrupt/resume. It does not automatically adopt LangGraph or replace product-domain authority.

The current runtime remains selected until an ADR records a go decision supported by every critical conformance case. If adopted, assign graph step/checkpoint ownership to one LangGraph adapter; Harnessa still gates claims, fencing, publication, budget and effects. Define how graph completion projects to domain transitions transactionally and idempotently. Never maintain competing mutable step state in two engines. Graph node replay cannot repeat an external effect; nodes must use existing effect intents and reconcile ambiguous outcomes.

Migration must identify checkpoint schema, mapping from run/attempt to graph thread/checkpoint, concurrent dispatch behavior, and supported resume semantics. Default rollout drains existing runs under the old runtime and routes new runs through one selected backend. Do not translate live checkpoints without a tested converter. Rollback stops new dispatch, reconciles active/unknown effects, and restores the prior compatible runtime; it cannot replay already published effects or erase audit evidence.

## Frameworks and typed adapter boundaries

Keep third-party types behind Harnessa ports: execution/session, specification, retrieval, UI projection, evaluation and model routing. Define application contracts in Pydantic/OpenAPI and generate TypeScript types through the existing contracts package. SDK object serialization must not become a second public API/schema authority.

- **ACP Python SDK:** use the official SDK for wire framing, generated schemas, capabilities and session operations. The trusted runner retains isolation, process lifecycle, output limits, fencing and publication policy.
- **AG-UI SDKs/codecs:** use compatible official Python and TypeScript packages at the browser boundary. Persist/replay Harnessa events; closing a browser connection never cancels the run. Do not expose private reasoning even if the SDK has reasoning event types.
- **LlamaIndex + Qdrant:** pilot ingestion/retrieval components behind one existing search service. Keep lexical retrieval usable without an embedding provider. Qdrant is the selected initial vector backend; pgvector is not a simultaneous implementation requirement. Harnessa supplies mandatory scope and policy filters and authorizes source opening. Index metadata is not an access grant. Route embedding/reranking calls through the existing model budget boundary.
- **Harness² / LiteLLM / OpenSpec:** integrate pinned upstream interfaces through existing adapters. Framework adoption must not duplicate their algorithms or introduce uncontrolled provider calls. MCP and A2A remain interoperability boundaries with Harnessa grants.

Context7 research identified the official ACP, AG-UI and Qdrant client repositories and inspected LangGraph 1.0.8 and LlamaIndex v0.14.6 reference docs. These are research references, not installed or mandated package versions. Verify Python 3.12, actual Next.js 16/React, database and protocol compatibility, licenses and supported APIs; pin exact packages/images during implementation.

## Core evidence contract and authority

Before a new subsystem emits results, extend the existing event/artifact contracts with stable task/run/attempt IDs, source-system IDs, revision/hash, occurred/recorded time, schema version, correlation/causation, scope and sensitivity/redaction labels. Keep unknown fields explicitly unknown. Source-specific records retain their authority: gateway usage is not task completion; a GitHub webhook is not current PR truth; a model score is not permission or acceptance.

Use existing PostgreSQL events/outbox/inbox and effect intents. Add metadata references instead of duplicating prompt content. Retain original evidence when correction/reconciliation arrives. Milestone 11 completes cross-system joins; earlier milestones already emit this core contract and later milestones add their own adapters without changing historical IDs.

## Costs, failures, and recovery

Apply project/task/run atomic reservations to all calls, retries, probes, classifiers, embeddings, compressors, judges and delegated agents. Keep actual, estimated and unknown costs distinct. Live calls require configured credentials and explicit spending authorization under an existing bounded policy; fixtures remain usable offline. Specify one retry owner per operation, finite attempts/deadlines, cancellation, partial-stream behavior and unknown-outcome reconciliation. A transport retry cannot replay an entire agent workflow or effect.

Enforce project/repository grants before subscriptions, retrieval and effects, including after revocation. Redact before logs/streaming; exclude secrets and hidden evaluation material from agent, framework, retrieval and candidate-editor access. Preserve immutable artifact hashes and revisions across retention, backup/restore, migration and rollback.

## Required milestone handoff and archive

Each prompt owns the subsystem named in its title and the minimum adapter/API/UI changes that connect it. Its report must name implemented outputs, exact pinned versions, commands/results, offline versus live evidence, migration/recovery/rollback behavior, configured resource/cost limits, unresolved external blockers and the next milestone’s prerequisite checks. An acceptance criterion is passed only by evidence; file presence and checked boxes do not establish completion. Optional live evidence remains explicitly pending where a prompt permits it; required external acceptance blocks completion.

The numbered sequence is the dependency order. Milestone 4 requires completed evidence for 1–3; every later milestone requires all preceding milestones. Earlier phases may define ports/fixtures for later systems but cannot require later implementations. Prompts 4–13 form the initial release; 14–17 extend that release. Archive only under the index lifecycle after acceptance; never modify archived historical prompts to match new numbering.

## Research references

- [ACP SDK quickstart](https://github.com/agentclientprotocol/python-sdk/blob/main/docs/quickstart.md)
- [AG-UI events](https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/sdk/js/core/events.mdx)
- [LangGraph checkpoint interface](https://github.com/langchain-ai/langgraph/blob/1.0.8/libs/checkpoint/README.md) and [PostgresSaver](https://github.com/langchain-ai/langgraph/blob/1.0.8/libs/checkpoint-postgres/README.md)
- [LlamaIndex Qdrant integration](https://github.com/run-llama/llama_index/blob/v0.14.6/docs/examples/vector_stores/Qdrant_using_qdrant_filters.ipynb)
- [Qdrant Python client](https://github.com/qdrant/qdrant-client)
