"""Immutable verification policy, run results, review bundles, and integration evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harnessa.db.base import Base, Scoped

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class VerificationManifestRevision(Scoped, Base):
    """An immutable administrator-approved command manifest for one repository."""

    __tablename__ = "verification_manifest_revisions"
    __table_args__ = (
        UniqueConstraint("repository_id", "revision", name="uq_verification_manifest_revision"),
        CheckConstraint("revision > 0", name="ck_verification_manifest_revision_positive"),
        Index("ix_verification_manifests_latest", "repository_id", "revision"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    protected_fixtures: Mapped[dict[str, str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    protected_fixture_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VerificationExecution(Scoped, Base):
    """Append-only verifier output bound to one run fence and exact result revision."""

    __tablename__ = "verification_executions"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "fencing_token",
            "manifest_revision_id",
            name="uq_verification_execution_identity",
        ),
        CheckConstraint(
            "attempt > 0 AND fencing_token > 0", name="ck_verification_execution_fence"
        ),
        CheckConstraint(
            "outcome IN ('passed','failed','manual_review_required','unverified')",
            name="ck_verification_execution_outcome",
        ),
        Index("ix_verification_execution_task", "project_id", "task_id", "created_at"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    manifest_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("verification_manifest_revisions.id", ondelete="RESTRICT")
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ReviewBundle(Scoped, Base):
    """Immutable content-addressed review package; status only tracks freshness."""

    __tablename__ = "review_bundles"
    __table_args__ = (
        UniqueConstraint("project_id", "bundle_digest", name="uq_review_bundle_digest"),
        UniqueConstraint("run_id", "fencing_token", name="uq_review_bundle_run_fence"),
        CheckConstraint("fencing_token > 0", name="ck_review_bundle_fence_positive"),
        CheckConstraint(
            "status IN ('prepared','published','invalidated')", name="ck_review_bundle_status"
        ),
        Index("ix_review_bundles_task", "project_id", "task_id", "created_at"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    verification_execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("verification_executions.id", ondelete="RESTRICT")
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    specification_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    specification_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    harness_revision: Mapped[str | None] = mapped_column(String(120), nullable=True)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="prepared", nullable=False)
    invalidated_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)


class ReviewDecision(Scoped, Base):
    """Immutable reviewer action bound to the precise bundle digest and head SHA."""

    __tablename__ = "review_decisions"
    __table_args__ = (
        UniqueConstraint("actor_id", "idempotency_key", name="uq_review_decision_idempotency"),
        CheckConstraint(
            "decision IN ('approve','request_changes','comment')", name="ck_review_decision_kind"
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    bundle_id: Mapped[UUID] = mapped_column(ForeignKey("review_bundles.id", ondelete="RESTRICT"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    bundle_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    github_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)


class IntegrationEvidence(Scoped, Base):
    """Recoverable publication/integration proof, separate from human approval."""

    __tablename__ = "integration_evidence"
    __table_args__ = (
        UniqueConstraint("project_id", "stable_key", name="uq_integration_evidence_key"),
        CheckConstraint("mode IN ('github','local_manual')", name="ck_integration_evidence_mode"),
        CheckConstraint(
            "status IN ('pending','confirmed','unknown','failed')",
            name="ck_integration_evidence_status",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    bundle_id: Mapped[UUID] = mapped_column(ForeignKey("review_bundles.id", ondelete="RESTRICT"))
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    integration_revision: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confirmed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PostIntegrationStep(Scoped, Base):
    """Separate retryable archive, learning, and cleanup outcomes after integration."""

    __tablename__ = "post_integration_steps"
    __table_args__ = (
        UniqueConstraint("integration_evidence_id", "kind", name="uq_post_integration_step"),
        CheckConstraint(
            "kind IN ('archive','learning','cleanup')", name="ck_post_integration_step_kind"
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','skipped')",
            name="ck_post_integration_step_status",
        ),
    )

    integration_evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("integration_evidence.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
