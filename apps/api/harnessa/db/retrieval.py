"""Durable retrieval provenance and per-project MCP grants/audit metadata."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harnessa.db.base import Base, Scoped

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class RetrievalSource(Scoped, Base):
    __tablename__ = "retrieval_sources"
    __table_args__ = (
        UniqueConstraint("project_id", "source_key", "revision", name="uq_retrieval_source_revision"),
        Index("ix_retrieval_sources_scope", "organization_id", "project_id", "state"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=True
    )
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    revision: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    license: Mapped[str] = mapped_column(String(120), nullable=False)
    retention: Mapped[str] = mapped_column(String(120), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(20), nullable=False)
    access_policy: Mapped[str] = mapped_column(String(120), nullable=False)
    connector_version: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="current")
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RetrievalChunk(Scoped, Base):
    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        UniqueConstraint("source_record_id", "chunk_key", name="uq_retrieval_chunk_key"),
        Index("ix_retrieval_chunks_scope", "organization_id", "project_id", "source_type"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=True
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("retrieval_sources.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    chunk_key: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="current")


class RetrievalSync(Scoped, Base):
    __tablename__ = "retrieval_syncs"
    __table_args__ = (Index("ix_retrieval_syncs_project", "project_id", "started_at"),)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=True
    )
    connector_id: Mapped[str] = mapped_column(String(80), nullable=False)
    connector_version: Mapped[str] = mapped_column(String(80), nullable=False)
    revision: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    considered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    indexed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    omitted_counts: Mapped[dict[str, int]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)


class MCPProjectGrant(Scoped, Base):
    __tablename__ = "mcp_project_grants"
    __table_args__ = (UniqueConstraint("project_id", "server_id", name="uq_mcp_project_server"),)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    server_id: Mapped[str] = mapped_column(String(120), nullable=False)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    read_only_tools: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    protocol_version: Mapped[str] = mapped_column(String(40), nullable=False)
    secret_reference: Mapped[str | None] = mapped_column(String(240), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPToolAudit(Scoped, Base):
    __tablename__ = "mcp_tool_audit"
    __table_args__ = (Index("ix_mcp_tool_audit_scope", "project_id", "created_at"),)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    server_id: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
