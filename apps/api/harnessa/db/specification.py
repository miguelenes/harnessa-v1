"""Harnessa-owned interview, question, artifact revision, and readiness records."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harnessa.db.base import Base, Scoped

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class SpecificationInterview(Scoped, Base):
    """Task-scoped interview metadata; artifact text remains in the Git workspace."""

    __tablename__ = "specification_interviews"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_spec_interviews_task"),
        UniqueConstraint("project_id", "idempotency_key", name="uq_spec_interviews_key"),
        UniqueConstraint("repository_id", "change_id", name="uq_spec_interviews_repository_change"),
        CheckConstraint("revision > 0", name="ck_spec_interview_revision_positive"),
        CheckConstraint("artifact_revision > 0", name="ck_spec_artifact_revision_positive"),
        CheckConstraint(
            "readiness_state IN ('NOT_EVALUATED','NOT_READY','READY','STALE')",
            name="ck_spec_interview_readiness_state",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="RESTRICT"), index=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    change_id: Mapped[str] = mapped_column(String(80), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    artifact_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_revision: Mapped[str] = mapped_column(String(80), default="m6-v1", nullable=False)
    readiness_state: Mapped[str] = mapped_column(
        String(20), default="NOT_EVALUATED", nullable=False
    )
    readiness_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON_DOCUMENT, nullable=True
    )
    architect_idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    architect_source_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    architect_artifact_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SpecificationQuestion(Scoped, Base):
    """Durable interview or run clarification question with revision provenance."""

    __tablename__ = "specification_questions"
    __table_args__ = (
        CheckConstraint(
            "classification IN ('blocking','nonblocking')",
            name="ck_spec_question_classification",
        ),
        CheckConstraint(
            "status IN ('open','answered','auto_resolved','stale')",
            name="ck_spec_question_status",
        ),
        CheckConstraint(
            "attempt IS NULL OR attempt > 0", name="ck_spec_question_attempt_positive"
        ),
    )

    interview_id: Mapped[UUID] = mapped_column(
        ForeignKey("specification_interviews.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    classification: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_assumption: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_options: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_DOCUMENT, default=list, nullable=False
    )
    affected_artifacts: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT, default=list, nullable=False
    )
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_DOCUMENT, default=list, nullable=False
    )


class SpecificationAnswer(Scoped, Base):
    """Append-only answer receipts; request keys make answer replay safe."""

    __tablename__ = "specification_answers"
    __table_args__ = (
        UniqueConstraint("interview_id", "idempotency_key", name="uq_spec_answer_key"),
        UniqueConstraint("question_id", name="uq_spec_answer_question"),
    )

    interview_id: Mapped[UUID] = mapped_column(
        ForeignKey("specification_interviews.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("specification_questions.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    answer_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )


class SpecificationArtifactRevision(Scoped, Base):
    """Immutable pointer to one committed OpenSpec bundle revision."""

    __tablename__ = "specification_artifact_revisions"
    __table_args__ = (
        UniqueConstraint("interview_id", "revision", name="uq_spec_artifact_revision"),
        UniqueConstraint("interview_id", "commit_sha", name="uq_spec_artifact_commit"),
    )

    interview_id: Mapped[UUID] = mapped_column(
        ForeignKey("specification_interviews.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    paths: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    change_summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class SpecificationReadinessEvaluation(Scoped, Base):
    """Immutable result bound to one artifact hash, Git base, and policy revision."""

    __tablename__ = "specification_readiness_evaluations"
    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 1", name="ck_spec_readiness_score_range"),
        CheckConstraint(
            "state IN ('NOT_READY','READY','STALE')", name="ck_spec_readiness_state"
        ),
    )

    interview_id: Mapped[UUID] = mapped_column(
        ForeignKey("specification_interviews.id", ondelete="CASCADE"), index=True
    )
    interview_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    gates: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    evaluated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    signed_off_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
