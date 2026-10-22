"""Compare lexical and Qdrant pilot retrieval on a fixed, synthetic corpus."""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import statistics
import time
from uuid import uuid4

from harnessa.adapters.retrieval_vector import LlamaIndexQdrantAdapter
from harnessa.services.retrieval_governance import (
    RetrievalScope,
    SourceDocument,
    lexical_search,
    split_chunks,
)
from llama_index.core.embeddings import MockEmbedding
from qdrant_client import QdrantClient


class FixtureEmbedding(MockEmbedding):
    """Local token-hash fixture; it is not a production or gateway embedding model."""

    def __init__(self) -> None:
        super().__init__(embed_dim=128)

    @property
    def dimension(self) -> int:
        return self.embed_dim

    @property
    def model_revision(self) -> str:
        return "fixture-token-hash-v1"

    def _vector(self, text: str) -> list[float]:
        values = [0.0] * self.embed_dim
        for token in re.findall(r"[a-z0-9]+", text.casefold()):
            digest = hashlib.sha256(token.encode()).digest()
            slot = int.from_bytes(digest[:4], "big") % self.embed_dim
            values[slot] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._vector(text)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._vector(text)


def _rank_metrics(ranked: list[str], expected: str, limit: int = 3) -> tuple[float, float]:
    top = ranked[:limit]
    recall = float(expected in top)
    reciprocal_rank = 1.0 / (top.index(expected) + 1) if expected in top else 0.0
    return recall, reciprocal_rank


def run_pilot(url: str) -> dict[str, object]:
    sources = (
        ("scheduler", "The Harnessa scheduler owns task run identity, leases, and fencing tokens."),
        ("authorization", "Project membership grants authorization before repository content is opened."),
        ("budget", "Embedding calls require a gateway-approved model and explicit cost budget."),
        ("cache", "RAG collections are isolated from semantic cache data and deletion policies."),
    )
    documents = tuple(
        SourceDocument(
            source_id=f"fixture-{name}",
            source_type="approved_docs",
            organization_id="fixture-org",
            project_id="fixture-project",
            repository_id="fixture-repository",
            source_uri=f"fixture://{name}",
            revision="fixture-corpus-v1",
            path=f"docs/{name}.md",
            content=content,
            license="synthetic-fixture",
            retention="run-only",
            sensitivity="internal",
            access_policy="fixture-scope-only",
            approved=True,
        )
        for name, content in sources
    )
    chunks = tuple(chunk for document in documents for chunk in split_chunks(document))
    scope = RetrievalScope(
        "fixture-org", "fixture-project", "fixture-repository", frozenset({"approved_docs"}), "fixture-v1"
    )
    queries = (
        ("scheduler authority run identity", "fixture-scheduler"),
        ("project membership authorization", "fixture-authorization"),
        ("embedding budget cost", "fixture-budget"),
    )
    lexical_latencies: list[float] = []
    vector_latencies: list[float] = []
    lexical_recall: list[float] = []
    vector_recall: list[float] = []
    lexical_mrr: list[float] = []
    vector_mrr: list[float] = []
    source_by_chunk = {chunk.chunk_id: chunk.source_id for chunk in chunks}
    client = QdrantClient(url=url, timeout=3)
    collection = f"harnessa_rag_m7_pilot_{uuid4().hex[:12]}"
    adapter = LlamaIndexQdrantAdapter(
        client=client, collection_name=collection, embed_model=FixtureEmbedding()
    )
    try:
        adapter.upsert(chunks)
        adapter.upsert(chunks)
        point_count_before_delete = client.count(collection_name=collection, exact=True).count
        for query, expected in queries:
            start = time.perf_counter()
            lexical = lexical_search(chunks, scope, query, 3)
            lexical_latencies.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            vector = adapter.search(query, scope, 3)
            vector_latencies.append((time.perf_counter() - start) * 1000)
            lexical_ranked = [hit.chunk.source_id for hit in lexical]
            vector_ranked = [source_by_chunk[chunk_id] for chunk_id, _ in vector]
            lex_recall, lex_mrr = _rank_metrics(lexical_ranked, expected)
            vec_recall, vec_mrr = _rank_metrics(vector_ranked, expected)
            lexical_recall.append(lex_recall)
            vector_recall.append(vec_recall)
            lexical_mrr.append(lex_mrr)
            vector_mrr.append(vec_mrr)

        points, _ = client.scroll(collection_name=collection, limit=10, with_payload=True)
        stored_text = any(
            isinstance(point.payload, dict)
            and ("text" in point.payload or "content" in point.payload or "page_content" in point.payload)
            for point in points
        )
        if stored_text:
            raise RuntimeError("Vector store unexpectedly persisted source text in Qdrant payloads")
        first_chunk = chunks[0]
        adapter.delete_revision(first_chunk.source_id, first_chunk.revision)
        point_count_after_delete = client.count(collection_name=collection, exact=True).count
        if point_count_before_delete != len(chunks) or point_count_after_delete != len(chunks) - 1:
            raise RuntimeError("Qdrant duplicate upsert or revision deletion did not converge")
        return {
            "corpus_revision": "fixture-corpus-v1",
            "documents": len(documents),
            "chunks": len(chunks),
            "idempotent_upsert_points": point_count_before_delete,
            "points_after_revision_delete": point_count_after_delete,
            "queries": len(queries),
            "lexical_recall_at_3": statistics.mean(lexical_recall),
            "vector_recall_at_3": statistics.mean(vector_recall),
            "lexical_mrr_at_3": statistics.mean(lexical_mrr),
            "vector_mrr_at_3": statistics.mean(vector_mrr),
            "lexical_latency_ms_median": statistics.median(lexical_latencies),
            "vector_latency_ms_median": statistics.median(vector_latencies),
            "embedding_cost_usd": 0,
            "embedding_evidence": "local deterministic fixture; no provider calls",
            "qdrant_payload_contains_source_text": stored_text,
            "limitation": "Synthetic four-document corpus and token-hash fixture do not establish production semantic quality.",
        }
    finally:
        client.delete_collection(collection_name=collection)
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    args = parser.parse_args()
    print(run_pilot(args.qdrant_url))


if __name__ == "__main__":
    main()
