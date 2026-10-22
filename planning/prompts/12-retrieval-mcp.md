# Prompt 12 — Governed retrieval and MCP context

Implement milestone 12 only after prompt 10 is complete and milestone 11 has established durable provenance contracts. Read the revised specification, architecture, compatibility matrix, prompts 03–11, completed milestone reports, and current implementation. Preserve unrelated work. RAG and MCP must improve agent context while remaining subordinate to repository authorization, Harnessa policy, and durable task state.

## Goal

Provide permission-aware retrieval over explicitly selected repository/specification history, verified run/evaluation evidence, and approved skills. Use MCP for typed tool access where it improves interoperability; do not route every internal service through MCP or make an LLM the authorization layer.

## Implement

1. Extend prompt 6's repository/OpenSpec search interface and source-citation contract; do not create a second search API, index authority, or conflicting corpus. Add versioned connectors for repository files, OpenSpec artifacts, approved documentation, prior verified runs, and skill/harness revisions. Each indexed chunk must retain organization/project/repository scope, source URI, commit/revision, content hash, timestamps, license/retention, sensitivity, and access policy. Exclude secrets, ignored files, hidden evaluation fixtures, unapproved repositories, and untrusted external corpora by default.
2. Extend the existing retrieval pipeline with deterministic lexical search first and an optional embedding/vector adapter behind its established interface. Support chunking, incremental indexing by changed-file/commit, deletion/tombstones, deduplication, and index rebuild. Pin embedding model/dimension and index schema; never mix incompatible vectors silently. Keep source text in the authorized artifact/source store and index references where practical.
3. Apply authorization before retrieval and again before opening source content. Enforce project/repository grants and path policy in the service layer; model filters are not a security boundary. Prevent cross-project cache leakage. Mark prompt-injection-bearing content as untrusted evidence and quote/cite retrieved sources with exact revision/path/line or artifact reference.
4. Add retrieval context budgets and deterministic ranking/filters. Emit a provenance manifest of query policy, sources considered, selected chunks, scores/rank, revisions, and content hashes, with privacy-safe query logging. Unknown or stale sources must not be presented as current truth.
5. Add scoped MCP server/tool registry with protocol negotiation, capability discovery, per-project grants, server/tool allowlists, secret references, timeouts, bounded output, audit records, and revocation. Prefer read-only tools for retrieval; mutations must call existing authorized domain commands/effect-intent services. MCP results remain untrusted data. If LiteLLM MCP Gateway is used, map gateway key/team/server scopes to Harnessa grants and do not mistake a gateway grant for repository authorization.
6. Expose retrieval and MCP status in the UI: indexed revision, freshness, coverage, failed syncs, permission scope, tool availability, and why a source was omitted. Include dry-run preview before indexing large repositories and explicit budget/resource limits.
7. Add checks for path traversal, malicious retrieved instructions, access revocation, deletion, stale index, cache isolation, connector outage, duplicate sync, MCP capability mismatch, tool timeout, oversized output, and revoked credentials.

## Acceptance

- A user with access to repository A cannot retrieve, infer via cache, or open content from repository B; revocation takes effect before the next query.
- Every answer using retrieval can expose citations tied to immutable source revisions and reproduce the selected context manifest.
- Repository updates and deletions converge idempotently; stale indexes and unavailable sources are visible.
- MCP tool discovery and invocation are scoped, logged, bounded, and revocable. No agent-authored content can grant access or invoke mutation endpoints outside the existing policy boundary.
- Hidden benchmark answers/graders and private credentials cannot enter the retrieval corpus or agent context.
- Offline checks cover retrieval and MCP contracts without requiring an embedding vendor or external MCP service.

Write `docs/integrations/retrieval-mcp.md`, document source policies, index rebuild/deletion, MCP grants, backup and migration behavior, and record evidence in `docs/milestones/12.md`. Archive only when acceptance is met.

## References to verify against pinned versions

- [LiteLLM MCP gateway](https://docs.litellm.ai/docs/mcp/) provides gateway-level server/key/team controls; these do not replace Harnessa project authorization.
- [Model Context Protocol](https://modelcontextprotocol.io/introduction) defines tool/resource interoperability. Negotiate protocol versions and inspect advertised capabilities rather than assuming feature support.
- Choose an embedding/vector technology only after measuring corpus size, update pattern, query latency, and deletion needs. Keep lexical retrieval usable when embeddings are unavailable.
