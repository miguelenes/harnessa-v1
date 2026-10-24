from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from harnessa.db.base import Base, Scoped


class Organization(Scoped, Base):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class User(Scoped, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="users_email_key"),)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Membership(Scoped, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        CheckConstraint(
            "role IN ('administrator', 'operator', 'reviewer', 'viewer')",
            name="ck_memberships_role",
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20), nullable=False)


class Project(Scoped, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    event_sequence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    execution_adapter: Mapped[str] = mapped_column(String(32), default="deployment-default", nullable=False)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Repository(Scoped, Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("project_id", "target_id"),)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    target_id: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), default="main", nullable=False)


class Task(Scoped, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_dispatch", "stage", "run_status", "available_at", "priority"),
        CheckConstraint(
            "stage IN ('BACKLOG','SPECIFYING','READY','IN_PROGRESS','VERIFYING','HUMAN_REVIEW','LEARNING','DONE','CLARIFYING','HARNESS_PROBE','REJECTED','CANCELLED','RETRY_EXHAUSTED')",
            name="ck_tasks_stage",
        ),
        CheckConstraint(
            "run_status IN ('QUEUED','RUNNING','WAITING_FOR_INPUT','PAUSED','FAILED','CANCELLED','SUCCEEDED')",
            name="ck_tasks_run_status",
        ),
        CheckConstraint(
            "return_stage IS NULL OR return_stage IN ('BACKLOG','SPECIFYING','READY','IN_PROGRESS','VERIFYING','HUMAN_REVIEW','LEARNING','DONE','CLARIFYING','HARNESS_PROBE','REJECTED','CANCELLED','RETRY_EXHAUSTED')",
            name="ck_tasks_return_stage",
        ),
        CheckConstraint("version > 0", name="ck_tasks_version_positive"),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), default="BACKLOG", nullable=False)
    run_status: Mapped[str] = mapped_column(String(32), default="QUEUED", nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    return_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    event_sequence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectMembership(Scoped, Base):
    __tablename__ = "project_memberships"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id"),
        CheckConstraint(
            "role IN ('administrator', 'operator', 'reviewer', 'viewer')",
            name="ck_project_memberships_role",
        ),
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20), nullable=False)


class IdempotencyRecord(Scoped, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("user_id", "key"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
