from __future__ import annotations

import pytest

MockEmbedding = pytest.importorskip("llama_index.core.embeddings").MockEmbedding

from harnessa.adapters.retrieval_vector import LlamaIndexQdrantAdapter
from harnessa.services.retrieval_governance import (
    RetrievalScope,
    SourceDocument,
    split_chunks,
)


class FixtureEmbedding(MockEmbedding):
    def __init__(self) -> None:
        super().__init__(embed_dim=8)

    @property
    def dimension(self) -> int:
        return self.embed_dim

    @property
    def model_revision(self) -> str:
        return "fixture-hash-embedding-v1"


def _doc(project_id: str, repository_id: str, content: str) -> SourceDocument:
    return SourceDocument(
        source_id=f"source-{project_id}",
        source_type="approved_docs",
        organization_id="org-a",
        project_id=project_id,
        repository_id=repository_id,
        source_uri=f"repo://{repository_id}/docs/guide.md",
        revision="revision-1",
        path="docs/guide.md",
        content=content,
        license="owner-approved",
        retention="until-disconnect",
        sensitivity="internal",
        access_policy="project-membership",
        approved=True,
    )


def test_optional_llamaindex_qdrant_pilot_enforces_project_filter_and_namespace() -> None:
    qdrant_client = pytest.importorskip("qdrant_client").QdrantClient(":memory:")
    adapter = LlamaIndexQdrantAdapter(
        client=qdrant_client,
        collection_name="harnessa_rag_test_v1",
        embed_model=FixtureEmbedding(),
    )
    project_a = split_chunks(_doc("project-a", "repo-a", "retrieval policy visible to project A"))
    project_b = split_chunks(_doc("project-b", "repo-b", "retrieval policy private to project B"))
    adapter.upsert(project_a + project_b)
    hits = adapter.search(
        "retrieval policy",
        RetrievalScope("org-a", "project-a", "repo-a", frozenset({"approved_docs"}), "policy-v1"),
        5,
    )
    assert hits
    assert {chunk_id for chunk_id, _ in hits} == {project_a[0].chunk_id}
    with pytest.raises(ValueError, match="namespace"):
        LlamaIndexQdrantAdapter(
            client=qdrant_client,
            collection_name="harnessa_litellm_cache_v1",
            embed_model=FixtureEmbedding(),
        )
    with pytest.raises(ValueError, match="dimension"):
        LlamaIndexQdrantAdapter(
            client=qdrant_client,
            collection_name="harnessa_rag_test_v2",
            embed_model=type("WrongDimension", (), {"dimension": 0, "model_revision": ""})(),
        )
