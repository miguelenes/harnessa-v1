"""Framework-neutral, repository-scoped evidence retrieval contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RepositoryCitation:
    revision: str
    path: str
    line: int
    snippet: str
    matched_terms: tuple[str, ...]
    untrusted_source_content: bool = True
    content_hash: str = ""


@dataclass(frozen=True)
class RepositorySearchResult:
    revision: str
    citations: tuple[RepositoryCitation, ...]
    context_manifest: RetrievalContextManifest | None = None


@dataclass(frozen=True)
class RetrievalSourceRecord:
    """Immutable, scoped provenance for a source revision; this is not an access grant."""

    source_id: str
    source_type: str
    source_uri: str
    organization_id: str
    project_id: str
    repository_id: str | None
    revision: str
    content_hash: str
    created_at: datetime
    license: str
    retention: str
    sensitivity: str
    access_policy: str
    stale: bool = False
    tombstoned: bool = False


@dataclass(frozen=True)
class RetrievalManifestItem:
    source_id: str
    source_uri: str
    revision: str
    path: str
    line: int
    content_hash: str
    rank: int
    score: int
    stale: bool
    untrusted_source_content: bool = True


@dataclass(frozen=True)
class RetrievalContextManifest:
    schema_version: int
    query_hash: str
    policy_revision: str
    organization_id: str
    project_id: str
    repository_id: str
    sources_considered: tuple[str, ...]
    selected: tuple[RetrievalManifestItem, ...]
    omitted: tuple[str, ...]
    max_context_bytes: int
    context_bytes: int


class RepositorySearchPort(Protocol):
    """Governed lexical retrieval with optional vector ranking behind Harnessa policy."""

    async def search(
        self, repository_target_id: str, default_branch: str, query: str, limit: int
    ) -> RepositorySearchResult: ...


class RepositoryRevisionSearchPort(Protocol):
    """Search current repository revisions; callers authorize again before opening sources."""

    async def search(
        self, repository_target_id: str, default_branch: str, query: str, limit: int
    ) -> RepositorySearchResult: ...
