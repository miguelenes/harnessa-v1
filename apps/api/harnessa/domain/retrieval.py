"""Framework-neutral, repository-scoped evidence retrieval contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RepositoryCitation:
    revision: str
    path: str
    line: int
    snippet: str
    matched_terms: tuple[str, ...]
    untrusted_source_content: bool = True


@dataclass(frozen=True)
class RepositorySearchResult:
    revision: str
    citations: tuple[RepositoryCitation, ...]


class RepositorySearchPort(Protocol):
    """Lexical now, governed retrieval later; citations never grant access."""

    async def search(
        self, repository_target_id: str, default_branch: str, query: str, limit: int
    ) -> RepositorySearchResult: ...
