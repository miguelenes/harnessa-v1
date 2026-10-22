"""Optional LlamaIndex/Qdrant adapter. Embeddings must be supplied by an approved gateway port."""

from __future__ import annotations

from typing import Protocol, cast
from uuid import NAMESPACE_URL, uuid5

from harnessa.services.retrieval_governance import (
    IndexedChunk,
    RetrievalScope,
    mandatory_vector_filter,
)


class ApprovedEmbeddingPort(Protocol):
    """Gateway-budgeted embedding capability; never instantiate a vendor model by default."""

    @property
    def dimension(self) -> int: ...

    @property
    def model_revision(self) -> str: ...

    def get_query_embedding(self, query: str) -> list[float]: ...

    def get_text_embedding(self, text: str) -> list[float]: ...


class LlamaIndexQdrantAdapter:
    """Qdrant vector pilot with mandatory service-generated project scope predicates."""

    def __init__(
        self,
        *,
        client: object,
        collection_name: str,
        embed_model: ApprovedEmbeddingPort,
        index_schema: str = "rag-v1",
    ) -> None:
        if not collection_name.startswith("harnessa_rag_") or "cache" in collection_name.casefold():
            raise ValueError("RAG collection names must use the isolated harnessa_rag_ namespace")
        if embed_model.dimension <= 0 or not embed_model.model_revision:
            raise ValueError("approved embedding model and dimension must be pinned")
        try:
            from llama_index.core import StorageContext, VectorStoreIndex
            from llama_index.core.base.embeddings.base import BaseEmbedding
            from llama_index.vector_stores.qdrant import QdrantVectorStore
            from qdrant_client import QdrantClient, models
        except ImportError as exc:
            raise RuntimeError("Install the optional retrieval extra to enable vector search") from exc

        qdrant = cast(QdrantClient, client)
        if not qdrant.collection_exists(collection_name):
            qdrant.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(
                    size=embed_model.dimension, distance=models.Distance.COSINE
                ),
            )
        collection = qdrant.get_collection(collection_name)
        vectors = collection.config.params.vectors
        if isinstance(vectors, dict) or vectors is None or getattr(vectors, "size", None) != embed_model.dimension:
            raise ValueError("Qdrant collection dimension does not match the pinned embedding model")
        self._client = qdrant
        self._models = models
        self._collection = collection_name
        self._embed_model = embed_model
        self.index_schema = index_schema
        self.model_revision = embed_model.model_revision
        self._store = QdrantVectorStore(
            client=qdrant,
            collection_name=collection_name,
            stores_text=False,
        )
        self._index = VectorStoreIndex(
            [],
            storage_context=StorageContext.from_defaults(vector_store=self._store),
            embed_model=cast(BaseEmbedding, embed_model),
        )
        indexed_fields = {
            "organization_id": models.PayloadSchemaType.KEYWORD,
            "project_id": models.PayloadSchemaType.KEYWORD,
            "repository_id": models.PayloadSchemaType.KEYWORD,
            "source_type": models.PayloadSchemaType.KEYWORD,
            "stale": models.PayloadSchemaType.BOOL,
            "hidden_evaluation": models.PayloadSchemaType.BOOL,
        }
        for field_name, field_schema in indexed_fields.items():
            try:
                qdrant.create_payload_index(
                    collection_name=collection_name,
                    field_name=field_name,
                    field_schema=field_schema,
                    wait=True,
                )
            except Exception as exc:
                # Existing payload indexes are expected; other errors are surfaced on use.
                if "already exists" not in str(exc).casefold():
                    raise

    def upsert(self, chunks: tuple[object, ...]) -> None:
        from llama_index.core.schema import TextNode

        nodes = []
        for raw in chunks:
            chunk = cast(IndexedChunk, raw)
            nodes.append(
                TextNode(
                    id_=str(uuid5(NAMESPACE_URL, chunk.chunk_id)),
                    text=chunk.content,
                    metadata={
                        "chunk_id": chunk.chunk_id,
                        "organization_id": chunk.organization_id,
                        "project_id": chunk.project_id,
                        "repository_id": chunk.repository_id,
                        "source_id": chunk.source_id,
                        "source_type": chunk.source_type,
                        "revision": chunk.revision,
                        "path": chunk.path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "content_hash": chunk.content_hash,
                        "sensitivity": chunk.sensitivity,
                        "access_policy": chunk.access_policy,
                        "stale": chunk.stale,
                        "hidden_evaluation": False,
                        "index_schema": self.index_schema,
                        "embedding_model": self.model_revision,
                    },
                )
            )
        if nodes:
            self._index.insert_nodes(nodes)

    def delete_revision(self, source_id: str, revision: str) -> None:
        self._client.delete(
            collection_name=self._collection,
            points_selector=self._models.Filter(
                must=[
                    self._models.FieldCondition(
                        key="source_id", match=self._models.MatchValue(value=source_id)
                    ),
                    self._models.FieldCondition(
                        key="revision", match=self._models.MatchValue(value=revision)
                    ),
                ]
            ),
            wait=True,
        )

    def search(self, query: str, scope: RetrievalScope, limit: int = 10) -> tuple[tuple[str, float], ...]:
        if not 1 <= limit <= 50:
            raise ValueError("vector result limit must be between 1 and 50")
        if scope.repository_id is None:
            raise ValueError("vector retrieval requires an explicitly authorized repository scope")
        filters = self._models.Filter(
            must=[
                self._models.FieldCondition(key="organization_id", match=self._models.MatchValue(value=scope.organization_id)),
                self._models.FieldCondition(key="project_id", match=self._models.MatchValue(value=scope.project_id)),
                self._models.FieldCondition(key="repository_id", match=self._models.MatchValue(value=scope.repository_id)),
                self._models.FieldCondition(key="source_type", match=self._models.MatchAny(any=sorted(scope.allowed_source_types))),
                self._models.FieldCondition(key="stale", match=self._models.MatchValue(value=False)),
                self._models.FieldCondition(key="hidden_evaluation", match=self._models.MatchValue(value=False)),
            ]
        )
        # LlamaIndex executes query embedding through the injected approved adapter.
        retriever = self._index.as_retriever(
            similarity_top_k=limit,
            vector_store_kwargs={"qdrant_filters": filters},
        )
        results = retriever.retrieve(query)
        return tuple(
            (str(item.node.metadata.get("chunk_id", "")), float(item.score or 0.0))
            for item in results
            if item.node.metadata.get("organization_id") == scope.organization_id
            and item.node.metadata.get("project_id") == scope.project_id
        )

    def mandatory_filter_document(self, scope: RetrievalScope) -> dict[str, object]:
        """Serializable audit view; never accepts caller-provided filter fragments."""
        return mandatory_vector_filter(scope)
