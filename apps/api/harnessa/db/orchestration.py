"""Durable orchestration records owned by PostgreSQL."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harnessa.db.base import Base, Scoped

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class TaskTransition(Scoped, Base):
    __tablename__ = "task_transitions"
    __table_args__ = (
        UniqueConstraint("actor_id", "command_key", name="uq_transition_actor_command"),
        UniqueConstraint("task_id", "version_after", name="uq_transition_task_version"),
        Index("ix_task_transitions_task_created", "task_id", "created_at"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    from_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    from_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    command: Mapped[str] = mapped_column(String(32), default="transition", nullable=False)
    command_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version_before: Mapped[int] = mapped_column(Integer, nullable=False)
    version_after: Mapped[int] = mapped_column(Integer, nullable=False)


class Run(Scoped, Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt", name="uq_run_task_attempt"),
        CheckConstraint("attempt > 0", name="ck_run_attempt_positive"),
        CheckConstraint("fencing_token >= 0", name="ck_run_fencing_nonnegative"),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','WAITING_FOR_INPUT','PAUSED','FAILED','CANCELLED','SUCCEEDED')",
            name="ck_runs_status",
        ),
        Index("ix_runs_task_created", "task_id", "created_at"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    retry_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Reverse pointer kept as metadata; checkpoints themselves are strongly run-scoped.
    checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    resume_from_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    adapter_name: Mapped[str] = mapped_column(String(100), nullable=False)


class RunCheckpoint(Scoped, Base):
    __tablename__ = "run_checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_checkpoint_run_sequence"),)

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resumable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RunArtifact(Scoped, Base):
    """A locally stored artifact published under the current run fencing token."""

    __tablename__ = "run_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "fencing_token", "digest", name="uq_run_artifact_fence_digest"),
        Index("ix_run_artifacts_scope", "project_id", "task_id", "run_id"),
        CheckConstraint("fencing_token > 0", name="ck_run_artifacts_fence_positive"),
        CheckConstraint("size_bytes >= 0", name="ck_run_artifacts_size_nonnegative"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(160), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(300), nullable=False)


class TaskLease(Scoped, Base):
    """One durable lease row per task; its fencing token is never reset."""

    __tablename__ = "task_leases"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_lease_task"),
        CheckConstraint("fencing_token >= 0", name="ck_lease_fencing_nonnegative"),
        Index("ix_leases_expiry", "expires_at"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    owner_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DomainEvent(Scoped, Base):
    __tablename__ = "domain_events"
    __table_args__ = (
        UniqueConstraint("project_id", "project_sequence", name="uq_event_project_sequence"),
        UniqueConstraint("task_id", "aggregate_sequence", name="uq_event_task_sequence"),
        Index("ix_events_project_retention", "project_id", "retention_until"),
    )

    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    aggregate_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    correlation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    causation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxMessage(Scoped, Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index("ix_outbox_due", "published_at", "next_attempt_at", "created_at"),
    )

    event_id: Mapped[UUID] = mapped_column(ForeignKey("domain_events.id", ondelete="CASCADE"), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConsumerInbox(Scoped, Base):
    __tablename__ = "consumer_inbox"
    __table_args__ = (UniqueConstraint("consumer", "event_id", name="uq_inbox_consumer_event"),)

    consumer: Mapped[str] = mapped_column(String(120), nullable=False)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("domain_events.id", ondelete="CASCADE"), index=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)


class EffectIntent(Scoped, Base):
    __tablename__ = "effect_intents"
    __table_args__ = (UniqueConstraint("stable_key", name="uq_effect_stable_key"),)

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeadLetter(Scoped, Base):
    __tablename__ = "dead_letters"
    __table_args__ = (Index("ix_dead_letters_status_created", "status", "created_at"),)

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("domain_events.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    replayed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
