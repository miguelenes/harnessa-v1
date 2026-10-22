# Prompt 7 of 13 — Governed retrieval and MCP context

Implement milestone 7 only after milestones 1–6 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, the planning index, and reports `docs/milestones/01.md` through `06.md`. Inspect the current code, versions, and worktree before editing; preserve unrelated changes. Complete this milestone and stop before milestone 8.

## Prerequisites and owned scope

All milestones through 6 have completed acceptance evidence. The existing task, authorization, artifact and model-budget services are operational. Extend the named subsystem through its current ports; use the shared decisions for failure/recovery, retention and cost rules. External services require configured access; deterministic fixtures cover unavailable integrations.

## Milestone 7 kickoff: reconcile the repository before feature work

Complete this gate before retrieval/MCP implementation. Use a clean isolated checkout based on the freshly fetched `origin/main`; verify its current branch is `main`, `HEAD` equals `origin/main`, and the worktree has no changes. Preserve unrelated user work in other checkouts.

1. Refresh the live GitHub inventory of every open PR and every local and remote branch, including automation branches. Audit each PR's exact head/base, diff, dependencies, review proposals/threads, required checks, and active branch protections. Begin with Milestone 6 PR #16, then process the remaining pending branches and PRs. Obtain independent review comments/proposals as described in the delivery contract, repair or rebase viable work, rerun required checks, and merge through repository protections. Do not use stale local refs as evidence.
2. Resolve stale, duplicate, or incompatible work without losing unique commits: identify its replacement or reason for rejection in the PR, close it only after review confirms it should not merge, and delete a source branch only after confirming its changes are merged or intentionally rejected and no open work depends on it. Do not delete protected/base branches or discard unmerged work. The gate passes only when GitHub reports zero open PRs and the only remaining repository branch is `main` (local and remote), with no pending local branches/worktrees. Record each merged/closed item, exact head and merge SHA where applicable, branch cleanup, and any blocker in `docs/milestones/07.md`.
3. After reconciliation, refresh `main` again and verify the M7 implementation checkout is clean `main` at the exact current `origin/main` SHA. Do not begin milestone 7 feature work while any extra open PR or branch remains; keep M7 blocked and record the unresolved item if a required protection or external action still prevents reconciliation.
4. Inventory active application targets and existing package-manager scripts. Add a small documented root command helper for supported development and verification commands against the selected app/package, including the Python API and active web, site, design-system, and contracts packages. Reuse `pnpm`/`uv` scripts as the source of truth; do not duplicate definitions or create an unrestricted shell wrapper. The helper must list supported targets/commands, run from the correct workspace, preserve output and exit status, reject unknown targets/commands, and work from a clean checkout with documented prerequisites. Record examples and verification in the milestone report.
5. Create or refine the root `AGENTS.md` as the concise, repository-grounded entry point for coding agents. Put high-priority rules first; identify the app/package map and verified commands; link to authoritative architecture, security, development, and milestone documents instead of copying them. Check each factual claim and command against tracked source/configuration, remove stale or conflicting guidance, and keep volatile details in their owning documents. Use short imperative instructions, clear headings, explicit source links, and no unsupported assumptions. Record the reviewed sources and material corrections in the milestone report.

These opening tasks are operational prerequisites for retrieval/MCP work. They do not authorize bypassing a required check, approval, or branch protection.

## Goal

Provide permission-aware retrieval over explicitly selected repository/specification history, verified run/evaluation evidence, and approved skills. Use MCP for typed tool access where it improves interoperability; do not route every internal service through MCP or make an LLM the authorization layer.

## Retrieval framework pilot

Pilot pinned `llama-index-core`, its Qdrant integration, and `qdrant-client` behind the existing retrieval interface. Use one ingestion/index authority with immutable source IDs, revisions, citations, deletion/tombstones, and context manifests. Qdrant is the first vector backend; provision a pinned persistent service with readiness, resource limits, backup/rebuild and collection migration instructions. Keep lexical search available without embeddings. Compare the framework adapter with the lexical baseline on a frozen corpus for retrieval quality, update/deletion correctness, latency, resource use and embedding cost; retain lexical operation if vector prerequisites are missing or the pilot fails acceptance.

Generate mandatory scope filters from Harnessa’s authorization context before any vector query and reauthorize before opening content. LlamaIndex metadata filters and Qdrant payloads cannot grant access. Inject only gateway-approved embedding/reranking clients; disable implicit vendor defaults and uncontrolled model calls. Record model/dimension, chunker, index and source revisions. RAG collections and future LiteLLM semantic-cache collections have distinct namespaces and deletion policies.

Acceptance must include grant revocation, deletion convergence, stale-source handling, attempted filter bypass, cross-project cache leakage, and exclusion of hidden evaluation material. Version and license checks precede adopting connectors; do not install the entire optional integration catalog.

## Implement

1. Extend prompt 6's repository/OpenSpec search interface and source-citation contract; do not create a second search API, index authority, or conflicting corpus. Add versioned connectors for repository files, OpenSpec artifacts, approved documentation, prior verified runs, and skill/harness revisions. Each indexed chunk must retain organization/project/repository scope, source URI, commit/revision, content hash, timestamps, license/retention, sensitivity, and access policy. Exclude secrets, ignored files, hidden evaluation fixtures, unapproved repositories, and untrusted external corpora by default.
2. Extend the existing retrieval pipeline with deterministic lexical search first and an optional embedding/vector adapter behind its established interface. Support chunking, incremental indexing by changed-file/commit, deletion/tombstones, deduplication, and index rebuild. Pin embedding model/dimension and index schema; never mix incompatible vectors silently. Keep source text in the authorized artifact/source store and index references where practical.
3. Apply authorization before retrieval and again before opening source content. Enforce project/repository grants and path policy in the service layer; model filters are not a security boundary. Prevent cross-project cache leakage. Mark prompt-injection-bearing content as untrusted evidence and quote/cite retrieved sources with exact revision/path/line or artifact reference.
4. Add retrieval context budgets and deterministic ranking/filters. Emit a provenance manifest of query policy, sources considered, selected chunks, scores/rank, revisions, and content hashes, with privacy-safe query logging. Unknown or stale sources must not be presented as current truth.
5. Add scoped MCP server/tool registry with protocol negotiation, capability discovery, per-project grants, server/tool allowlists, secret references, timeouts, bounded output, audit records, and revocation. Prefer read-only tools for retrieval; mutations must call existing authorized domain commands/effect-intent services. MCP results remain untrusted data. If LiteLLM MCP Gateway is used, map gateway key/team/server scopes to Harnessa grants and do not mistake a gateway grant for repository authorization.
6. Expose retrieval and MCP status through authenticated API contracts for milestone 8 UI wiring: indexed revision, freshness, coverage, failed syncs, permission scope, tool availability, and why a source was omitted. Include dry-run preview before indexing large repositories and explicit budget/resource limits.
7. Add checks for path traversal, malicious retrieved instructions, access revocation, deletion, stale index, cache isolation, connector outage, duplicate sync, MCP capability mismatch, tool timeout, oversized output, and revoked credentials.

## Acceptance

- A user with access to repository A cannot retrieve, infer via cache, or open content from repository B; revocation takes effect before the next query.
- Every answer using retrieval can expose citations tied to immutable source revisions and reproduce the selected context manifest.
- Repository updates and deletions converge idempotently; stale indexes and unavailable sources are visible.
- MCP tool discovery and invocation are scoped, logged, bounded, and revocable. No agent-authored content can grant access or invoke mutation endpoints outside the existing policy boundary.
- Hidden benchmark answers/graders and private credentials cannot enter the retrieval corpus or agent context.
- Offline checks cover retrieval and MCP contracts without requiring an embedding vendor or external MCP service.

Write `docs/integrations/retrieval-mcp.md`, document source policies, index rebuild/deletion, MCP grants, backup and migration behavior, and record evidence in `docs/milestones/07.md`. Archive only when acceptance is met.

## References to verify against pinned versions

- [LiteLLM MCP gateway](https://docs.litellm.ai/docs/mcp/) provides gateway-level server/key/team controls; these do not replace Harnessa project authorization.
- [Model Context Protocol](https://modelcontextprotocol.io/introduction) defines tool/resource interoperability. Negotiate protocol versions and inspect advertised capabilities rather than assuming feature support.
- Measure the selected Qdrant/LlamaIndex pilot against corpus size, update pattern, query latency, and deletion needs before default enablement. Keep lexical retrieval usable when embeddings are unavailable.

## Delivery contract

Own only the subsystem named by this prompt and its existing adapters/UI projections. Preserve the shared authority, contract-generation, cost, recovery, and retention rules in `planning/architecture-decisions.md`. Produce the implementation, focused acceptance evidence, updated compatibility/setup notes, and `docs/milestones/07.md`. Record offline versus live results separately, exact versions/configuration and source revisions, pending external setup, recovery/rollback commands, and the next milestone’s prerequisites. Missing required evidence keeps the milestone active.

For review, assign an independent code-review agent to inspect the exact PR head. The reviewer posts progress and each change proposal directly in the PR conversation, with severity, affected path/line, evidence, impact, and a suggested change; a no-findings review posts a concise final comment explaining why no proposal is needed. The maintainer reads every proposal and either accepts it or responds with evidence and rationale. Limit each proposal's discussion to three PR comment/reply messages total, then record a final accepted or rejected decision and its rationale. Apply accepted changes on the PR branch and obtain a fresh review of the final head; resolve every review thread. A conversation comment is not a formal approval. Never self-approve or bypass repository protections.

During this run, put all milestone deliverables—including implementation, documentation, migrations, and `docs/milestones/07.md`—in one PR. Obtain an independent code review, resolve accepted findings, and merge the reviewed final head in this run after required checks and repository protections pass. Include the PR URL, reviewed head SHA, final review disposition, check results, and merge commit SHA in the final run summary. A blocked review or merge leaves milestone 7 incomplete and active; do not archive its prompt.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced; the future archive target is `planning/archive/completed/07-retrieval-mcp.md`. Never run the next milestone as part of this request.
