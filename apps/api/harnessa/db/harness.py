"""Durable Harness² experiment, evidence and promotion records."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from harnessa.db.base import Base, Scoped
from harnessa.db.orchestration import JSON_DOCUMENT


class HarnessPolicy(Scoped, Base):
    """Project-scoped limits and immutable evaluator/promotion guard references."""

    __tablename__ = "harness_policies"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_harness_policy_project"),
        CheckConstraint("max_k BETWEEN 1 AND 3", name="ck_harness_policy_k"),
        CheckConstraint(
            "max_probe_concurrency BETWEEN 1 AND 12", name="ck_harness_policy_concurrency"
        ),
        CheckConstraint("reviewed_promotion IS TRUE", name="ck_harness_policy_reviewed"),
        CheckConstraint("cost_multiplier >= 1", name="ck_harness_policy_cost_multiplier"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_k: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_probe_concurrency: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    reviewed_promotion: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cost_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), default=Decimal("1.25"), nullable=False
    )
    regression_gate_revision: Mapped[str] = mapped_column(
        String(80), default="fixed-regression-v1", nullable=False
    )
    security_gate_revision: Mapped[str] = mapped_column(
        String(80), default="fixed-security-v1", nullable=False
    )
    evaluator_policy_revision: Mapped[str] = mapped_column(
        String(80), default="immutable-holdout-v1", nullable=False
    )


class HarnessExperiment(Scoped, Base):
    """One durable Propose–Probe–Compose experiment bound to task/base/spec identity."""

    __tablename__ = "harness_experiments"
    __table_args__ = (
        UniqueConstraint("project_id", "stable_key", name="uq_harness_experiment_key"),
        CheckConstraint(
            "mode IN ('parallel','sequential','both')", name="ck_harness_experiment_mode"
        ),
        CheckConstraint("k BETWEEN 1 AND 3", name="ck_harness_experiment_k"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','blocked')",
            name="ck_harness_experiment_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_harness_experiment_attempts"),
        Index("ix_harness_experiment_work", "status", "created_at"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    evaluation_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    stable_key: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    phase: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    specification_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    specification_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    harness2_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    baseline_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_storage_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    result_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationRun(Scoped, Base):
    """Immutable grouped evaluator manifest over existing durable Harness² experiments."""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        UniqueConstraint("project_id", "stable_key", name="uq_evaluation_run_key"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','blocked')",
            name="ck_evaluation_run_status",
        ),
        CheckConstraint("max_cost_usd > 0", name="ck_evaluation_run_cost_positive"),
        Index("ix_evaluation_run_project_created", "project_id", "created_at"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    stable_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_ids: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HarnessProbeRun(Scoped, Base):
    """Redacted baseline, candidate or composed-run evidence; artifacts remain in CAS."""

    __tablename__ = "harness_probe_runs"
    __table_args__ = (
        UniqueConstraint("experiment_id", "label", name="uq_harness_probe_label"),
        CheckConstraint(
            "phase IN ('baseline','candidate','composed')", name="ck_harness_probe_phase"
        ),
        CheckConstraint(
            "status IN ('succeeded','failed','cancelled','unknown')", name="ck_harness_probe_status"
        ),
        CheckConstraint(
            "wall_ms IS NULL OR wall_ms >= 0", name="ck_harness_probe_wall_nonnegative"
        ),
        CheckConstraint(
            "tokens IS NULL OR tokens >= 0", name="ck_harness_probe_tokens_nonnegative"
        ),
        CheckConstraint(
            "cost_usd IS NULL OR cost_usd >= 0", name="ck_harness_probe_cost_nonnegative"
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("harness_experiments.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    delivered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    wall_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    route_alias: Mapped[str | None] = mapped_column(String(120), nullable=True)
    image_revision: Mapped[str | None] = mapped_column(String(300), nullable=True)
    tool_revision: Mapped[str | None] = mapped_column(String(120), nullable=True)
    harness_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_storage_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    trace_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    component_diff: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    outcome_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class HarnessExperience(Scoped, Base):
    """Immutable skill/lesson artifact versions with mutable review lifecycle state."""

    __tablename__ = "harness_experiences"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "experience_key", "version", name="uq_harness_experience_version"
        ),
        CheckConstraint("version > 0", name="ck_harness_experience_version"),
        CheckConstraint(
            "status IN ('candidate','active','rejected','superseded','stale')",
            name="ck_harness_experience_status",
        ),
        CheckConstraint(
            "source_kind IN ('generated','imported','operator_authored')",
            name="ck_harness_experience_source_kind",
        ),
        CheckConstraint(
            "scope_type IN ('task','repository','project','organization')",
            name="ck_harness_experience_scope_type",
        ),
        CheckConstraint(
            "trust_level IN ('untrusted','reviewed')",
            name="ck_harness_experience_trust_level",
        ),
        CheckConstraint(
            "conflict_state IN ('clear','conflict')",
            name="ck_harness_experience_conflict_state",
        ),
        Index("ix_harness_experience_scope", "project_id", "repository_scope", "status"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    source_task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    source_experiment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("harness_experiments.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    source_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    source_evaluation_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    experience_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    repository_scope: Mapped[str] = mapped_column(String(200), nullable=False)
    domain_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    cue: Mapped[str] = mapped_column(String(1000), nullable=False)
    lesson: Mapped[str] = mapped_column(String(2000), nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    conflict_key: Mapped[str] = mapped_column(String(120), nullable=False)
    conflict_state: Mapped[str] = mapped_column(String(16), default="clear", nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), default="generated", nullable=False)
    author: Mapped[str] = mapped_column(String(200), default="unknown", nullable=False)
    producer: Mapped[str] = mapped_column(String(200), default="harnessa", nullable=False)
    source_author_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    scope_type: Mapped[str] = mapped_column(String(16), default="repository", nullable=False)
    scope_id: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    license_id: Mapped[str] = mapped_column(String(120), default="UNKNOWN", nullable=False)
    dependencies: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    trust_level: Mapped[str] = mapped_column(String(16), default="untrusted", nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    artifact_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_storage_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    artifact_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence_rationale: Mapped[str] = mapped_column(
        Text, default="Legacy experience; confidence is not established.", nullable=False
    )
    compatibility_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    stale_reason: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expiry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)


_IMMUTABLE_EXPERIENCE_FIELDS = (
    "organization_id",
    "project_id",
    "source_task_id",
    "source_experiment_id",
    "source_run_id",
    "source_evaluation_run_id",
    "experience_key",
    "version",
    "repository_scope",
    "domain_scope",
    "cue",
    "lesson",
    "evidence_refs",
    "candidate_digest",
    "conflict_key",
    "conflict_state",
    "source_kind",
    "author",
    "producer",
    "source_author_id",
    "scope_type",
    "scope_id",
    "license_id",
    "dependencies",
    "allowed_tools",
    "trust_level",
    "artifact_path",
    "artifact_digest",
    "artifact_storage_key",
    "artifact_size_bytes",
    "confidence_rationale",
    "compatibility_snapshot",
    "expiry_at",
    "policy_revision",
)


@event.listens_for(HarnessExperience, "before_update")
def _reject_harness_experience_content_update(_mapper: Any, _connection: Any, target: HarnessExperience) -> None:
    state = inspect(target)
    changed = [
        name for name in _IMMUTABLE_EXPERIENCE_FIELDS
        if state.attrs[name].history.has_changes()
    ]
    if changed:
        raise ValueError("Harness experience content and provenance are immutable")


class HarnessPromotion(Scoped, Base):
    """Append-only promote/rollback decision bound to exact experience versions."""

    __tablename__ = "harness_promotions"
    __table_args__ = (
        UniqueConstraint("project_id", "stable_key", name="uq_harness_promotion_key"),
        CheckConstraint("action IN ('promote','rollback')", name="ck_harness_promotion_action"),
        CheckConstraint(
            "decision IN ('approved','rejected')", name="ck_harness_promotion_decision"
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    experience_id: Mapped[UUID] = mapped_column(
        ForeignKey("harness_experiences.id", ondelete="RESTRICT"), index=True
    )
    previous_experience_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("harness_experiences.id", ondelete="RESTRICT"), nullable=True
    )
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    stable_key: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    gate_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )


class SkillConflictResolution(Base):
    """Append-only administrator decision that retires a conflicting candidate group."""

    __tablename__ = "skill_conflict_resolutions"
    __table_args__ = (
        UniqueConstraint("project_id", "stable_key", name="uq_skill_conflict_resolution_key"),
        CheckConstraint("action = 'reject_group'", name="ck_skill_conflict_resolution_action"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    target_experience_id: Mapped[UUID] = mapped_column(
        ForeignKey("harness_experiences.id", ondelete="RESTRICT")
    )
    conflict_key: Mapped[str] = mapped_column(String(120), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    candidate_ids: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
