from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

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


class OrchestrationOperation(StrEnum):
    START = "start"
    CHECKPOINT = "checkpoint"
    INTERRUPT = "interrupt"
    ANSWER = "answer"
    RESUME = "resume"
    CANCEL = "cancel"
    RECONCILE = "reconcile"


class OrchestrationOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RECONCILED = "reconciled"


class EvidenceValueStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    REDACTED = "redacted"


class EvidenceSensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class EvidenceRedactionStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    APPLIED = "applied"
    PENDING = "pending"


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
    tool_calls: list[dict[str, Any]] | None = None


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
    model_config = ConfigDict(from_attributes=True)

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


class RunOrchestrationIdentity(BaseModel):
    """Harnessa-owned identity and fence carried through every runtime operation."""

    task_id: UUID
    run_id: UUID
    attempt: int = Field(ge=1)
    fencing_token: int = Field(ge=1)


class RunCheckpointReference(BaseModel):
    """Runtime-neutral reference to a durable Harnessa checkpoint."""

    checkpoint_id: UUID
    sequence: int = Field(ge=1)
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunStartRequest(BaseModel):
    identity: RunOrchestrationIdentity
    idempotency_key: str = Field(min_length=1, max_length=200)
    input: dict[str, Any]
    resume_from: RunCheckpointReference | None = None


class RunCheckpointWrite(BaseModel):
    identity: RunOrchestrationIdentity
    idempotency_key: str = Field(min_length=1, max_length=200)
    state: dict[str, Any]


class RunInterruptRequest(BaseModel):
    identity: RunOrchestrationIdentity
    idempotency_key: str = Field(min_length=1, max_length=200)
    question_id: UUID
    question_revision: int = Field(ge=1)
    prompt: dict[str, Any]
    answer_schema: dict[str, Any]


class RunAnswerRequest(BaseModel):
    identity: RunOrchestrationIdentity
    idempotency_key: str = Field(min_length=1, max_length=200)
    question_id: UUID
    question_revision: int = Field(ge=1)
    answer: dict[str, Any]


class RunResumeRequest(BaseModel):
    identity: RunOrchestrationIdentity
    idempotency_key: str = Field(min_length=1, max_length=200)
    checkpoint: RunCheckpointReference | None = None
    answer_idempotency_key: str | None = Field(default=None, max_length=200)


class RunCancelRequest(BaseModel):
    identity: RunOrchestrationIdentity
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class RunReconcileRequest(BaseModel):
    identity: RunOrchestrationIdentity
    idempotency_key: str = Field(min_length=1, max_length=200)
    stable_effect_keys: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_effect_keys(self) -> RunReconcileRequest:
        if len(set(self.stable_effect_keys)) != len(self.stable_effect_keys):
            raise ValueError("stable effect keys must be unique")
        return self


class RunOrchestrationReceipt(BaseModel):
    """Idempotent result of an orchestration port operation."""

    identity: RunOrchestrationIdentity
    operation: OrchestrationOperation
    idempotency_key: str = Field(min_length=1, max_length=200)
    outcome: OrchestrationOutcome
    checkpoint: RunCheckpointReference | None = None
    question_id: UUID | None = None
    question_revision: int | None = Field(default=None, ge=1)
    event_id: UUID | None = None
    recorded_at: datetime
    detail: str | None = Field(default=None, max_length=1000)


class EvidenceTextValue(BaseModel):
    status: EvidenceValueStatus
    value: str | None = None

    @model_validator(mode="after")
    def validate_status_value(self) -> EvidenceTextValue:
        if (self.status == EvidenceValueStatus.KNOWN) != (self.value is not None):
            raise ValueError("known evidence text requires a value; unknown or redacted text omits it")
        return self


class EvidenceUuidValue(BaseModel):
    status: EvidenceValueStatus
    value: UUID | None = None

    @model_validator(mode="after")
    def validate_status_value(self) -> EvidenceUuidValue:
        if (self.status == EvidenceValueStatus.KNOWN) != (self.value is not None):
            raise ValueError("known evidence UUID requires a value; unknown or redacted UUID omits it")
        return self


class EvidenceIntegerValue(BaseModel):
    status: EvidenceValueStatus
    value: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_status_value(self) -> EvidenceIntegerValue:
        if (self.status == EvidenceValueStatus.KNOWN) != (self.value is not None):
            raise ValueError("known evidence integer requires a value; unknown or redacted integer omits it")
        return self


class EvidenceTimestampValue(BaseModel):
    status: EvidenceValueStatus
    value: datetime | None = None

    @model_validator(mode="after")
    def validate_status_value(self) -> EvidenceTimestampValue:
        if (self.status == EvidenceValueStatus.KNOWN) != (self.value is not None):
            raise ValueError("known evidence time requires a value; unknown or redacted time omits it")
        return self


class CoreEvidenceEnvelope(BaseModel):
    """Source-linked evidence metadata; unknown and redacted values are distinguishable."""

    schema_version: int = Field(ge=1)
    source_system: str = Field(min_length=1, max_length=120)
    source_record_id: EvidenceTextValue
    task_id: EvidenceUuidValue
    run_id: EvidenceUuidValue
    attempt: EvidenceIntegerValue
    revision: EvidenceTextValue
    content_sha256: EvidenceTextValue
    occurred_at: EvidenceTimestampValue
    recorded_at: datetime
    correlation_id: EvidenceUuidValue
    causation_id: EvidenceUuidValue
    scope: list[str] = Field(min_length=1, max_length=32)
    sensitivity: EvidenceSensitivity
    redaction_status: EvidenceRedactionStatus


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


class SpecificationInterviewCreate(BaseModel):
    topic: str = Field(min_length=8, max_length=10_000)
    repository_id: UUID


class SpecificationInterviewRead(BaseModel):
    id: UUID
    task_id: UUID
    project_id: UUID
    repository_id: UUID
    topic: str
    revision: int
    artifact_revision: int
    base_sha: str
    head_sha: str
    artifact_hash: str
    readiness_state: str
    policy_revision: str


class SpecificationQuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    interview_id: UUID
    task_id: UUID
    run_id: UUID | None = None
    attempt: int | None = None
    fencing_token: int | None = None
    source_revision: int
    source: str
    classification: str
    status: str
    question: str
    proposed_assumption: str | None = None
    recommended_options: list[dict[str, Any]] = Field(default_factory=list)
    affected_artifacts: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class SpecificationAnswerCreate(BaseModel):
    expected_revision: int = Field(ge=1)
    answer: str = Field(min_length=1, max_length=10_000)
    changes_scope: bool = False


class SpecificationAnswerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    question_id: UUID
    actor_id: UUID | None = None
    answer_revision: int
    answer: str
    provenance: dict[str, Any]
    created_at: datetime


class SpecificationAnswerResult(BaseModel):
    answer: SpecificationAnswerRead
    interview: SpecificationInterviewRead


class SpecificationArtifactUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    proposal_md: str = Field(min_length=1, max_length=100_000)
    spec_md: str = Field(min_length=1, max_length=100_000)
    design_md: str = Field(min_length=1, max_length=100_000)
    tasks_md: str = Field(min_length=1, max_length=100_000)


class SpecificationArtifactRead(BaseModel):
    interview_id: UUID
    revision: int
    base_sha: str
    commit_sha: str
    content_hash: str
    paths: list[str]
    change_summary: str
    diff: str = Field(max_length=100_000)
    proposal_md: str = Field(max_length=100_000)
    spec_md: str = Field(max_length=100_000)
    design_md: str = Field(max_length=100_000)
    tasks_md: str = Field(max_length=100_000)


class SpecificationReadinessRead(BaseModel):
    interview_id: UUID
    interview_revision: int
    artifact_revision: int
    content_hash: str
    base_sha: str
    policy_revision: str
    score: float = Field(ge=0, le=1)
    state: str
    gates: dict[str, Any]
    signed_off_by: UUID | None = None


class SpecificationReadinessSignoff(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_artifact_revision: int = Field(ge=1)


class SpecificationArchitectRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    authorize_model_generation: bool = False


class SpecificationArchitectRead(BaseModel):
    interview_id: UUID
    revision: int
    artifact_revision: int
    questions: list[SpecificationQuestionRead]


class SpecificationExplorerEntry(BaseModel):
    revision: str
    path: str
    section: str
    archived: bool = False


class SpecificationExplorerRead(BaseModel):
    revision: str
    entries: list[SpecificationExplorerEntry]


class RepositorySearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=20, ge=1, le=50)


class RepositoryCitation(BaseModel):
    revision: str
    path: str
    line: int = Field(ge=1)
    snippet: str = Field(max_length=800)
    matched_terms: list[str]
    untrusted_source_content: bool = True


class RepositorySearchResponse(BaseModel):
    revision: str
    results: list[RepositoryCitation]
