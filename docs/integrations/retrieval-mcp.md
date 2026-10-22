# Governed retrieval and MCP

## Authority and source policy

Harnessa's existing authenticated specification-search route remains the only search API. The API checks project membership and repository ownership before the workspace adapter reads the current default-branch Git tree. It checks access again immediately before reading content. Citations identify the exact commit, path and line; returned text is untrusted evidence and does not change grants or policy.

The repository connector is versioned as `git-tree-v1`. It reads tracked regular Git blobs only, with limits of 2,500 files, 32,000,000 aggregate bytes, 512,000 bytes per file and 2,000,000 bytes per document. Hidden evaluation/grader paths, common credential filenames, detected secret-like content, traversal, symlinks, build outputs, dependencies and ignored/untracked files are omitted. Approved `docs/`, `planning/`, `openspec/`, application/package paths, `skills-lock.json`, and individually named `.agents/skills/**/SKILL.md` files are eligible. Skill paths require the project-keyed `RETRIEVAL_APPROVED_SKILL_PATHS_JSON` deployment setting; the setting grants indexing eligibility only and never grants a user access.

The index stores source and chunk metadata, hashes and line ranges, not source text. The source store remains Git. A dry-run sync is the default. An explicit sync creates immutable source-revision records, deduplicates repeats, marks changed prior records stale and deleted paths tombstoned, and records failed connector attempts without clearing the last successful index. Current freshness means “last sync only”; it is not a claim that Git was checked at response time. Query manifests hash the query, identify policy and repository revision, and include selected source hashes and omitted context-budget entries. Query text and retrieved content are not written to application logs.

The repository/OpenSpec/docs/approved-skill paths share the Git-tree connector. A verified-run connector is not enabled: the current schema has successful run/artifact records but no durable verification evidence record, and run success alone is not accepted as verification. Do not index run artifacts until that evidence and its authorization/retention contract exist. No external corpus connector is configured.

## Lexical search and optional vector pilot

Lexical repository search remains available independently of the optional retrieval extra. The optional packages are `llama-index-core==0.14.25`, `llama-index-vector-stores-qdrant==0.10.4`, and `qdrant-client==1.19.1`, installed with `uv sync --extra retrieval`. The adapter requires an injected Harnessa-approved embedding implementation, pins its model revision and dimension, uses `harnessa_rag_*` collections, checks vector dimensions, indexes mandatory organization/project/repository/source/stale/evaluation payload fields, and refuses a project-wide query without an explicit repository scope. Source text is not stored in Qdrant payloads. RAG and semantic-cache namespaces remain separate.

Qdrant's optional Compose profile uses the locally validated image `docker.io/qdrant/qdrant:v1.19.2@sha256:390ca16351185f9913c23d0c95ded0cdf2ce17e525dd7fbe58d3339c9c80e633`, persistent volume `qdrantdata`, a loopback-only HTTP port, a readiness check, and 1 GiB / 1 CPU / 128 PID bounds. Start it with `podman compose --project-name harnessa -f infra/compose.yaml --profile retrieval up -d qdrant`; check readiness with `curl --fail http://127.0.0.1:${QDRANT_PORT:-6333}/healthz`.

The pilot can be repeated against that local service with `PYTHONPATH=apps/api uv run --extra retrieval python scripts/m7_retrieval_pilot.py --qdrant-url http://127.0.0.1:6333`. It creates and deletes a unique temporary collection over a four-document synthetic corpus. Its token-hash embedding is deterministic test code, not a semantic model, a gateway call, or evidence of production retrieval quality. It compares recall@3, MRR@3 and median query latency, verifies that source text is absent from Qdrant payloads, and reports zero fixture embedding cost. Production vector search stays disabled until an approved embedding route passes through Harnessa's model-budget boundary and a representative frozen-corpus evaluation justifies enablement. Status reports the specific omission reason.

## MCP server registry, grants and calls

`MCP_SERVERS_JSON` is deployment-owned JSON mapping stable server IDs to HTTPS Streamable HTTP endpoints. Plain HTTP is accepted only for loopback. Redirect following is disabled. The project administrator creates a project grant; requested server IDs and read-only tool classifications must already appear in the deployment registry and `MCP_READ_ONLY_TOOLS_JSON`. Optional environment-variable secret references must also be listed for that exact server in deployment-owned `MCP_SECRET_REFERENCES_JSON`, for example `{"docs":{"DOCS_MCP_TOKEN"}}` (JSON uses arrays: `{"docs":["DOCS_MCP_TOKEN"]}`). A project administrator cannot select another process environment variable. Credential values never enter the database or API contracts. Do not place secret values in any JSON setting.

The client supports MCP `2026-07-28` stateless discovery plus the handshake-era versions `2025-11-25`, `2025-06-18`, `2025-03-26`, and `2024-11-05`. A project grant pins the protocol version; modern `server/discover` or legacy `initialize` must confirm that exact version. Modern requests carry protocol/client metadata on each request and the required method/name HTTP routing headers. See the official [2026-07-28 discovery contract](https://modelcontextprotocol.io/specification/2026-07-28/server/discover). The client checks the server's advertised tool capability, lists tools, validates the selected tool schema and arguments, and invokes only tools explicitly classified read-only. Mutations must use existing authorized Harnessa domain/effect services. Each request is capped at 16,000 argument bytes, 15 seconds, 64,000 response bytes and 256 discovered tools. Per-project grant, role and tool checks run before invocation; membership and grant are re-read before returning the remote result. Audit rows retain hashes, outcome, duration and safe identifiers, not arguments or result text. Remote content is untrusted.

No live MCP endpoint was configured or contacted during M7. Status distinguishes deployment configuration, project grants, unverified discovery and the absence of a live endpoint; offline MockTransport tests do not establish remote interoperability.

## Backup, rebuild, migration and rollback

PostgreSQL is authoritative for retrieval metadata, sync outcomes, MCP grants and audit records. Back it up using the existing PostgreSQL backup procedure together with any Qdrant volume backup. To make a consistent local Qdrant volume archive, stop only the retrieval-profile service, export its named volume, and restart it:

```sh
podman compose --project-name harnessa -f infra/compose.yaml --profile retrieval stop qdrant
podman volume export harnessa_qdrantdata --output qdrantdata.tar
podman compose --project-name harnessa -f infra/compose.yaml --profile retrieval start qdrant
```

Keep the archive encrypted and access controlled. Restore only after stopping the service: preserve the current volume, import the archive into a new empty named volume, point the Compose service at that volume, and verify `/healthz` and collection dimensions before reopening traffic. Do not overwrite the only current volume.

To rebuild the pilot, stop Harnessa vector reads, create a new versioned `harnessa_rag_<schema>` collection, re-index the authorized current source revisions with the same pinned embedding model/dimension and chunk schema, compare counts and scope filters, then switch configuration. Delete the old collection only after the new collection passes checks. Deletion from Git is reconciled by a successful repository sync and tombstones in PostgreSQL; vector deletion must use the same source ID/revision pair. A Qdrant collection migration is a create/rebuild/verify/switch operation, not an in-place dimension change.

Rollback the database change from `apps/api` with a PostgreSQL backup first, then `DATABASE_URL=<target> uv run python -m alembic downgrade d6a5e7c4b209`; this removes the M7 metadata and grant tables. Keep the Qdrant backup until the API rollback is verified. To disable the pilot, stop the retrieval profile and keep lexical search enabled; vector retrieval remains disabled in the current API because there is no approved embedding route.

## Fixture versus live evidence

Unit/integration fixtures cover path and secret exclusions, policy scoping, revocation, idempotent indexing, tombstones, stale records, connector outage, duplicate sync, nested MCP schemas, protocol/capability mismatch, deadlines, output limits and cache/project isolation. The Qdrant pilot script exercises the pinned local Qdrant service and a deterministic local embedding fixture only. No model provider, embedding provider, configured external MCP endpoint, or user repository was called for this evidence.
