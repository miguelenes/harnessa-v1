from __future__ import annotations

from enum import StrEnum
from datetime import datetime
from typing import Any
from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ModelProtocol(StrEnum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class ModelPoolKind(StrEnum):
    INTERACTIVE = "interactive"
    PROBE = "probe"


class RouteRequirements(BaseModel):
    protocol: ModelProtocol
    streaming: bool = False
    tools: bool = False
    minimum_context: int = Field(default=1, ge=1, le=2_000_000)
    usage_reporting: bool = True
    cancellation: bool = False


class ModelMessage(BaseModel):
    role: str = Field(min_length=1, max_length=40)
    content: str | list[dict[str, Any]] | None = None
    name: str | None = Field(default=None, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=200)


class ModelRouteRequest(BaseModel):
    task_id: UUID | None = None
    run_id: UUID | None = None
    pool: ModelPoolKind = ModelPoolKind.INTERACTIVE
    model_alias: str | None = Field(default=None, min_length=1, max_length=120)
    requirements: RouteRequirements
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    estimated_input_tokens: int = Field(ge=1, le=2_000_000)
    stream: bool = False
    messages: list[ModelMessage] | None = None
    input: str | list[dict[str, Any]] | None = None
    system: str | None = Field(default=None, max_length=100_000)
    tools: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_protocol_payload(self) -> ModelRouteRequest:
        if self.requirements.protocol == ModelProtocol.RESPONSES:
            if self.input is None:
                raise ValueError("Responses API requires an input")
        elif not self.messages:
            raise ValueError("Chat and Anthropic Messages APIs require at least one message")
        if self.stream and not self.requirements.streaming:
            raise ValueError("Streaming requests must require streaming capability")
        if self.tools and not self.requirements.tools:
            raise ValueError("Tool requests must require tool capability")
        return self

    def to_provider_payload(self, model: str) -> dict[str, Any]:
        if self.requirements.protocol == ModelProtocol.CHAT_COMPLETIONS:
            if not self.messages:
                raise ValueError("Chat Completions requires at least one message")
            payload: dict[str, Any] = {
                "model": model,
                "messages": [message.model_dump(exclude_none=True) for message in self.messages],
                "max_tokens": self.max_output_tokens,
                "stream": self.stream,
            }
            if self.stream:
                payload["stream_options"] = {"include_usage": True}
        elif self.requirements.protocol == ModelProtocol.RESPONSES:
            if self.input is None:
                raise ValueError("Responses API requires an input")
            payload = {
                "model": model,
                "input": self.input,
                "max_output_tokens": self.max_output_tokens,
                "stream": self.stream,
            }
            if self.system:
                payload["instructions"] = self.system
        else:
            if not self.messages:
                raise ValueError("Anthropic Messages requires at least one message")
            payload = {
                "model": model,
                "messages": [message.model_dump(exclude_none=True) for message in self.messages],
                "max_tokens": self.max_output_tokens,
                "stream": self.stream,
            }
            if self.system:
                payload["system"] = self.system
        if self.tools:
            payload["tools"] = self.tools
        return payload


class ModelProviderAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    provider: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_-]*$")
    secret_ref: str = Field(min_length=5, max_length=200, pattern=r"^env:[A-Z][A-Z0-9_]*$")


class ModelProviderAccountRead(ModelProviderAccountCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    enabled: bool


class ModelDeploymentCapabilities(BaseModel):
    protocols: list[ModelProtocol] = Field(min_length=1)
    streaming: bool = False
    tools: bool = False
    usage_reporting: bool = False
    cancellation: bool = False


class ModelDeploymentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    provider_account_id: UUID
    actual_model: str = Field(min_length=1, max_length=200)
    proxy_model: str = Field(min_length=1, max_length=120)
    capabilities: ModelDeploymentCapabilities
    context_window: int = Field(ge=1, le=2_000_000)
    input_cost_per_million: Decimal = Field(ge=0, max_digits=18, decimal_places=8)
    output_cost_per_million: Decimal = Field(ge=0, max_digits=18, decimal_places=8)
    price_revision: str = Field(min_length=1, max_length=100)
    max_concurrency: int = Field(default=4, ge=1, le=1000)


class ModelDeploymentRead(ModelDeploymentCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    enabled: bool
    cooldown_until: datetime | None = None
    failure_count: int = 0


class ModelAliasCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    description: str = Field(default="", max_length=500)
    deployment_ids: list[UUID] = Field(min_length=1, max_length=20)


class ModelAliasRead(BaseModel):
    id: UUID
    name: str
    description: str
    enabled: bool
    deployment_ids: list[UUID]


class ModelPoolRouteCreate(BaseModel):
    model_alias_id: UUID
    priority: int = Field(ge=0, le=1000)


class ModelPoolRead(BaseModel):
    id: UUID
    name: str
    kind: ModelPoolKind
    max_concurrency: int
    interactive_reserve: int
    probe_overflow_limit: int
    active_interactive: int
    active_probe: int


class ModelGatewayConfiguration(BaseModel):
    pools: list[ModelPoolRead]
    accounts: list[ModelProviderAccountRead]
    deployments: list[ModelDeploymentRead]
    aliases: list[ModelAliasRead]


class ModelBudgetPolicyWrite(BaseModel):
    project_limit_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    task_limit_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    run_limit_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    project_token_limit: int = Field(gt=0, le=10**12)
    task_token_limit: int = Field(gt=0, le=10**12)
    run_token_limit: int = Field(gt=0, le=10**12)
    max_steps: int = Field(gt=0, le=100_000)
    max_wall_seconds: int = Field(gt=0, le=604_800)
    max_output_tokens: int = Field(gt=0, le=1_000_000)
    max_attempts: int = Field(default=3, ge=1, le=5)


class ModelBudgetPolicyRead(ModelBudgetPolicyWrite):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID


class ModelUsageRead(BaseModel):
    project_id: UUID
    billed_usd: Decimal
    estimated_usd: Decimal
    input_tokens: int
    output_tokens: int
    unknown_usage_requests: int
    pending_reservations: int


class ModelRequestLedgerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    request_id: UUID
    attempt: int
    event_type: str
    project_id: UUID
    task_id: UUID | None = None
    run_id: UUID | None = None
    account_id: UUID | None = None
    deployment_id: UUID | None = None
    pool_name: str
    model_alias: str
    actual_model: str | None = None
    request_identifier: str | None = None
    route_decision: str
    price_revision: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_known: bool
    latency_ms: int | None = None
    billed_usd: Decimal | None = None
    estimated_usd: Decimal | None = None
    http_status: int | None = None
    error_code: str | None = None
    details: dict[str, Any]
    created_at: datetime


class ModelDoctorRead(BaseModel):
    status: str
    gateway_ready: bool
    configured_accounts: int
    configured_deployments: int
    configured_aliases: int
    pools: list[str]
    provider_credentials_checked: bool = False
    live_request_sent: bool = False
    findings: list[str] = Field(default_factory=list)


class ModelLiveCheckRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=120)
    authorize_paid_request: bool


class ModelLiveCheckRead(BaseModel):
    outcome: str
    request_id: UUID
    deployment: str | None = None
    provider_account: str | None = None
    actual_model: str | None = None
    usage_known: bool = False
    billed_usd: Decimal | None = None
    estimated_usd: Decimal | None = None
    error_code: str | None = None


class ModelInvocationRead(BaseModel):
    request_id: UUID
    model_alias: str
    actual_model: str
    deployment: str
    provider_account: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_known: bool = False
    billed_usd: Decimal | None = None
    estimated_usd: Decimal | None = None
    route_decisions: list[str]
    response: dict[str, Any]


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
