"""Deterministic source policy, lexical indexing, and scoped MCP invocation contracts."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Protocol

MAX_DOCUMENT_BYTES = 2_000_000
MAX_CHUNK_BYTES = 4_000
MAX_TOOL_OUTPUT_BYTES = 64_000
MAX_TOOL_TIMEOUT_SECONDS = 15.0
MODERN_MCP_PROTOCOL = "2026-07-28"
SUPPORTED_MCP_PROTOCOLS = (
    MODERN_MCP_PROTOCOL,
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
_SECRET_WORDS = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|credential)\s*[:=]|"
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
)
_HIDDEN_EVAL_PARTS = frozenset(
    {".evals", "evals", "evaluation", "benchmarks", "fixtures-private", "grader", "golden"}
)
_SECRET_NAMES = frozenset({".env", ".env.local", "id_rsa", "id_ed25519", "credentials.json"})


def safe_retrieval_content(content: str) -> bool:
    """Apply the same secret-content exclusion at every retrieval boundary."""
    return _SECRET_WORDS.search(content) is None


@dataclass(frozen=True)
class RetrievalScope:
    organization_id: str
    project_id: str
    repository_id: str | None
    allowed_source_types: frozenset[str]
    policy_revision: str


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    source_type: str
    organization_id: str
    project_id: str
    repository_id: str | None
    source_uri: str
    revision: str
    path: str
    content: str
    license: str
    retention: str
    sensitivity: str
    access_policy: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    approved: bool = False
    hidden_evaluation: bool = False
    stale: bool = False


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    source_id: str
    revision: str
    path: str
    start_line: int
    end_line: int
    content_hash: str
    content: str
    source_type: str
    organization_id: str
    project_id: str
    repository_id: str | None
    sensitivity: str
    access_policy: str
    stale: bool
    untrusted: bool = True


@dataclass(frozen=True)
class LexicalHit:
    chunk: IndexedChunk
    score: int
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, object]
    read_only: bool = True


@dataclass(frozen=True)
class MCPGrant:
    organization_id: str
    project_id: str
    server_id: str
    allowed_tools: frozenset[str]
    protocol_version: str
    secret_reference: str | None
    revoked: bool = False


class MCPToolClient(Protocol):
    async def invoke(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, object],
        protocol_version: str,
        secret_reference: str | None,
    ) -> bytes: ...


class VectorSearchPort(Protocol):
    async def search(
        self, query_vector: list[float], scope: RetrievalScope, limit: int
    ) -> list[tuple[str, float]]: ...


def eligible_path(path: str, *, approved_external: bool = False) -> bool:
    """Reject traversal, hidden evaluation and credential paths before reading/indexing."""
    if not path or "\\" in path or "\x00" in path:
        return False
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return False
    if candidate.name.casefold() in _SECRET_NAMES:
        return False
    if any(part.casefold() in _HIDDEN_EVAL_PARTS for part in candidate.parts):
        return False
    if any(part.startswith(".") for part in candidate.parts) and not (
        approved_external
        and len(candidate.parts) >= 4
        and candidate.parts[:2] == (".agents", "skills")
        and candidate.name == "SKILL.md"
    ):
        return False
    if approved_external:
        return True
    if path == "skills-lock.json":
        return True
    return path.startswith(("openspec/", "docs/", "planning/", "apps/", "packages/"))


def split_chunks(document: SourceDocument, max_bytes: int = MAX_CHUNK_BYTES) -> tuple[IndexedChunk, ...]:
    if max_bytes < 256 or max_bytes > MAX_DOCUMENT_BYTES:
        raise ValueError("chunk byte bound must be between 256 and 2,000,000")
    raw = document.content.encode("utf-8")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("document exceeds the 2,000,000-byte indexing limit")
    if not eligible_path(document.path, approved_external=document.approved):
        return ()
    if not document.approved or document.hidden_evaluation or document.stale:
        return ()
    if not safe_retrieval_content(document.content):
        return ()
    lines = document.content.splitlines()
    chunks: list[IndexedChunk] = []
    current: list[str] = []
    start = 1
    size = 0
    for line_number, line in enumerate(lines, start=1):
        line_size = len(line.encode("utf-8")) + 1
        if current and size + line_size > max_bytes:
            chunks.append(_make_chunk(document, current, start, line_number - 1))
            current, size, start = [], 0, line_number
        if line_size > max_bytes:
            line = line.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            line_size = len(line.encode("utf-8"))
        current.append(line)
        size += line_size
    if current:
        chunks.append(_make_chunk(document, current, start, len(lines)))
    return tuple(chunks)


def _make_chunk(document: SourceDocument, lines: list[str], start: int, end: int) -> IndexedChunk:
    content = "\n".join(lines)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    stable_id = hashlib.sha256(
        f"{document.source_id}:{document.revision}:{document.path}:{start}:{digest}".encode()
    ).hexdigest()
    return IndexedChunk(
        stable_id,
        document.source_id,
        document.revision,
        document.path,
        start,
        end,
        digest,
        content,
        document.source_type,
        document.organization_id,
        document.project_id,
        document.repository_id,
        document.sensitivity,
        document.access_policy,
        document.stale,
    )


def lexical_search(
    chunks: tuple[IndexedChunk, ...], scope: RetrievalScope, query: str, limit: int
) -> tuple[LexicalHit, ...]:
    if not 1 <= limit <= 50 or len(query.encode("utf-8")) > 2_000:
        raise ValueError("query or result limit exceeds the retrieval bound")
    if not query.strip():
        return ()
    terms = tuple(dict.fromkeys(term.casefold() for term in re.findall(r"[\w-]+", query)))
    hits: list[LexicalHit] = []
    for chunk in chunks:
        if (
            chunk.organization_id != scope.organization_id
            or chunk.project_id != scope.project_id
            or chunk.repository_id != scope.repository_id
            or chunk.source_type not in scope.allowed_source_types
            or chunk.stale
        ):
            continue
        folded = chunk.content.casefold()
        matched = tuple(term for term in terms if term in folded)
        if matched:
            hits.append(LexicalHit(chunk, sum(folded.count(term) for term in matched), matched))
    hits.sort(key=lambda hit: (-hit.score, hit.chunk.path, hit.chunk.start_line, hit.chunk.chunk_id))
    return tuple(hits[:limit])


def reconcile_revision(
    previous: tuple[IndexedChunk, ...], current: tuple[IndexedChunk, ...], revision: str
) -> tuple[tuple[IndexedChunk, ...], frozenset[str]]:
    """Replace a source snapshot idempotently and return tombstoned chunk IDs."""
    current_ids = {chunk.chunk_id for chunk in current}
    tombstones = frozenset(
        chunk.chunk_id for chunk in previous if chunk.revision != revision or chunk.chunk_id not in current_ids
    )
    unique = {chunk.chunk_id: chunk for chunk in current}
    return tuple(sorted(unique.values(), key=lambda chunk: (chunk.path, chunk.start_line))), tombstones


def mandatory_vector_filter(scope: RetrievalScope) -> dict[str, object]:
    """Generate scope filters in Harnessa; callers cannot supply or weaken this filter."""
    if scope.repository_id is None:
        raise ValueError("vector retrieval requires an explicitly authorized repository scope")
    return {
        "must": [
            {"key": "organization_id", "match": {"value": scope.organization_id}},
            {"key": "project_id", "match": {"value": scope.project_id}},
            {"key": "repository_id", "match": {"value": scope.repository_id}},
            {"key": "source_type", "match": {"any": sorted(scope.allowed_source_types)}},
            {"key": "stale", "match": {"value": False}},
            {"key": "hidden_evaluation", "match": {"value": False}},
        ],
    }


def assert_protocol_compatible(grant: MCPGrant, server_protocol_version: str) -> None:
    if grant.revoked:
        raise PermissionError("MCP project grant has been revoked")
    if grant.protocol_version not in SUPPORTED_MCP_PROTOCOLS:
        raise ValueError("Project grant pins an unsupported MCP protocol version")
    if server_protocol_version != grant.protocol_version:
        raise ValueError("MCP server protocol version does not match the project grant")


async def invoke_mcp_tool(
    client: MCPToolClient,
    *,
    grant: MCPGrant,
    authenticated_project_id: str,
    discovered_tool: MCPTool,
    arguments: dict[str, object],
    server_protocol_version: str,
    timeout_seconds: float = MAX_TOOL_TIMEOUT_SECONDS,
    output_limit_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> bytes:
    """Fail closed on scope, revocation, capability or output bounds before returning data."""
    assert_protocol_compatible(grant, server_protocol_version)
    if grant.project_id != authenticated_project_id:
        raise PermissionError("MCP grant does not cover this project")
    if discovered_tool.name not in grant.allowed_tools:
        raise PermissionError("MCP tool is not allowlisted for this project")
    if not discovered_tool.read_only:
        raise PermissionError("MCP mutations must use an authorized Harnessa domain command")
    if timeout_seconds <= 0 or timeout_seconds > MAX_TOOL_TIMEOUT_SECONDS:
        raise ValueError("MCP timeout exceeds the 15-second policy bound")
    if not 1 <= output_limit_bytes <= MAX_TOOL_OUTPUT_BYTES:
        raise ValueError("MCP output limit exceeds the 64,000-byte policy bound")
    result = await asyncio.wait_for(
        client.invoke(
            grant.server_id,
            discovered_tool.name,
            arguments,
            grant.protocol_version,
            grant.secret_reference,
        ),
        timeout=timeout_seconds,
    )
    if len(result) > output_limit_bytes:
        raise ValueError("MCP tool output exceeds the configured byte limit")
    return result
