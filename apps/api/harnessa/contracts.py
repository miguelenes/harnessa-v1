from enum import StrEnum
from datetime import datetime
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class TaskStage(StrEnum):
    BACKLOG = "BACKLOG"
    SPECIFYING = "SPECIFYING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    LEARNING = "LEARNING"
    DONE = "DONE"
    CLARIFYING = "CLARIFYING"
    HARNESS_PROBE = "HARNESS_PROBE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"


class Role(StrEnum):
    ADMINISTRATOR = "administrator"
    OPERATOR = "operator"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class EventEnvelope(BaseModel):
    event_id: UUID
    event_type: str
    event_version: int = 1
    schema_version: int = 1
    organization_id: UUID
    project_id: UUID
    task_id: UUID | None = None
    run_id: UUID | None = None
    sequence: int
    actor_id: UUID | None = None
    occurred_at: datetime
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Identity(BaseModel):
    id: UUID
    email: str
    organization_id: UUID
    role: Role


class AuthorizationContext(BaseModel):
    """Resolved internal authority for one organization and optional project scope."""

    identity: Identity
    project_id: UUID | None = None
    project_role: Role | None = None
    scopes: list[str] = Field(default_factory=list)


class TaskState(BaseModel):
    """Versioned lifecycle snapshot; execution status is independent of board stage."""

    task_id: UUID
    stage: TaskStage
    run_status: RunStatus
    blocked_reason: str | None = None
    return_stage: TaskStage | None = None
    version: int = Field(ge=1)


class SpecificationRevision(BaseModel):
    id: UUID
    project_id: UUID
    repository_id: UUID
    revision: str
    content_hash: str
    base_sha: str
    policy_revision: str


class ArtifactMetadata(BaseModel):
    id: UUID
    project_id: UUID
    task_id: UUID
    sha256: str
    size_bytes: int
    media_type: str
    retention_until: datetime | None = None
    redacted: bool = True


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)


class ProjectRead(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID


class RepositoryCreate(BaseModel):
    target_id: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=200)
    default_branch: str = "main"


class RepositoryRead(RepositoryCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
