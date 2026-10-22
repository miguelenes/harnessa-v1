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


class RetryClass(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    PERMANENT = "permanent"
    OPERATOR = "operator"


class TaskCommand(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RETRY = "retry"


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


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20000)
    priority: int = Field(default=0, ge=-100, le=100)


class TaskRead(TaskCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    stage: TaskStage
    run_status: RunStatus
    blocked_reason: str | None = None
    return_stage: TaskStage | None = None
    version: int
    created_at: datetime
    updated_at: datetime


class TaskTransitionRequest(BaseModel):
    target_stage: TaskStage
    reason: str = Field(min_length=1, max_length=1000)
    return_stage: TaskStage | None = None


class TaskCommandRequest(BaseModel):
    command: TaskCommand
    reason: str = Field(min_length=1, max_length=1000)


class RecoveryRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class TaskTransitionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    task_id: UUID
    actor_id: UUID | None
    from_stage: TaskStage | None
    to_stage: TaskStage
    from_run_status: RunStatus | None
    to_run_status: RunStatus | None
    reason: str
    command: str
    version_before: int
    version_after: int
    created_at: datetime


class DispatchRebuildResult(BaseModel):
    rebuilt: int


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    task_id: UUID
    attempt: int
    status: RunStatus
    retry_class: RetryClass | None = None
    error_code: str | None = None
    error_message: str | None = None
    fencing_token: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    checkpoint_id: UUID | None = None


class EventReplayItem(BaseModel):
    cursor: int
    event: EventEnvelope


class EventReplayPage(BaseModel):
    items: list[EventReplayItem]
    next_cursor: int | None
    replay_limit: int


class DeadLetterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    task_id: UUID
    run_id: UUID | None = None
    event_id: UUID | None = None
    kind: str
    reason: str
    payload: dict[str, Any]
    status: str
    created_at: datetime


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
