"""Model routing metadata, spend reservations, and immutable request audit events."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from harnessa.db.base import Base, Scoped
from harnessa.db.orchestration import JSON_DOCUMENT


class ModelPool(Scoped, Base):
    __tablename__ = "model_pools"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_model_pool_org_name"),
        CheckConstraint("kind IN ('interactive', 'probe')", name="ck_model_pool_kind"),
        CheckConstraint("max_concurrency > 0", name="ck_model_pool_capacity_positive"),
        CheckConstraint("interactive_reserve >= 0", name="ck_model_pool_reserve_nonnegative"),
        CheckConstraint("probe_overflow_limit >= 0", name="ck_model_pool_probe_overflow_nonnegative"),
        CheckConstraint("active_interactive >= 0", name="ck_model_pool_active_interactive_nonnegative"),
        CheckConstraint("active_probe >= 0", name="ck_model_pool_active_probe_nonnegative"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    interactive_reserve: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    probe_overflow_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_interactive: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_probe: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProviderAccount(Scoped, Base):
    __tablename__ = "model_provider_accounts"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_model_provider_account_name"),
        CheckConstraint("provider <> ''", name="ck_model_provider_name_nonempty"),
        CheckConstraint("secret_ref LIKE 'env:%'", name="ck_model_account_secret_ref"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ModelDeployment(Scoped, Base):
    __tablename__ = "model_deployments"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_model_deployment_name"),
        UniqueConstraint("provider_account_id", "proxy_model", name="uq_deployment_proxy_model"),
        CheckConstraint("context_window > 0", name="ck_deployment_context_positive"),
        CheckConstraint("input_cost_per_million >= 0", name="ck_deployment_input_price_nonnegative"),
        CheckConstraint("output_cost_per_million >= 0", name="ck_deployment_output_price_nonnegative"),
        CheckConstraint("failure_count >= 0", name="ck_deployment_failure_count_nonnegative"),
        CheckConstraint("max_concurrency > 0", name="ck_deployment_capacity_positive"),
        CheckConstraint("active_interactive >= 0", name="ck_deployment_active_interactive_nonnegative"),
        CheckConstraint("active_probe >= 0", name="ck_deployment_active_probe_nonnegative"),
        Index("ix_model_deployments_org_available", "organization_id", "enabled", "cooldown_until"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    provider_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_provider_accounts.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    actual_model: Mapped[str] = mapped_column(String(200), nullable=False)
    proxy_model: Mapped[str] = mapped_column(String(120), nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    context_window: Mapped[int] = mapped_column(Integer, nullable=False)
    input_cost_per_million: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    output_cost_per_million: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    price_revision: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    active_interactive: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_probe: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ModelAlias(Scoped, Base):
    __tablename__ = "model_aliases"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_model_alias_name"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AliasDeployment(Scoped, Base):
    __tablename__ = "model_alias_deployments"
    __table_args__ = (
        UniqueConstraint("alias_id", "deployment_id", name="uq_alias_deployment"),
        UniqueConstraint("alias_id", "priority", name="uq_alias_deployment_priority"),
        Index("ix_model_alias_deployments_order", "alias_id", "priority"),
    )

    alias_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_aliases.id", ondelete="CASCADE"), index=True
    )
    deployment_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_deployments.id", ondelete="CASCADE"), index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False)


class PoolAlias(Scoped, Base):
    __tablename__ = "model_pool_aliases"
    __table_args__ = (
        UniqueConstraint("pool_id", "alias_id", name="uq_pool_alias"),
        UniqueConstraint("pool_id", "priority", name="uq_pool_alias_priority"),
        Index("ix_model_pool_aliases_order", "pool_id", "priority"),
    )

    pool_id: Mapped[UUID] = mapped_column(ForeignKey("model_pools.id", ondelete="CASCADE"), index=True)
    alias_id: Mapped[UUID] = mapped_column(ForeignKey("model_aliases.id", ondelete="CASCADE"), index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)


class ModelBudgetPolicy(Scoped, Base):
    __tablename__ = "model_budget_policies"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_model_budget_project"),
        CheckConstraint("project_limit_usd > 0", name="ck_budget_project_limit_positive"),
        CheckConstraint("task_limit_usd > 0", name="ck_budget_task_limit_positive"),
        CheckConstraint("run_limit_usd > 0", name="ck_budget_run_limit_positive"),
        CheckConstraint("probe_project_limit_usd > 0", name="ck_budget_probe_project_limit_positive"),
        CheckConstraint("probe_task_limit_usd > 0", name="ck_budget_probe_task_limit_positive"),
        CheckConstraint("probe_run_limit_usd > 0", name="ck_budget_probe_run_limit_positive"),
        CheckConstraint("project_token_limit > 0", name="ck_budget_project_tokens_positive"),
        CheckConstraint("task_token_limit > 0", name="ck_budget_task_tokens_positive"),
        CheckConstraint("run_token_limit > 0", name="ck_budget_run_tokens_positive"),
        CheckConstraint("probe_project_token_limit > 0", name="ck_budget_probe_project_tokens_positive"),
        CheckConstraint("probe_task_token_limit > 0", name="ck_budget_probe_task_tokens_positive"),
        CheckConstraint("probe_run_token_limit > 0", name="ck_budget_probe_run_tokens_positive"),
        CheckConstraint("max_steps > 0", name="ck_budget_max_steps_positive"),
        CheckConstraint("max_wall_seconds > 0", name="ck_budget_max_wall_positive"),
        CheckConstraint("max_output_tokens > 0", name="ck_budget_max_output_positive"),
        CheckConstraint("max_attempts BETWEEN 1 AND 5", name="ck_budget_attempts_bounded"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    project_limit_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    task_limit_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    run_limit_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    probe_project_limit_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    probe_task_limit_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    probe_run_limit_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    project_token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    probe_project_token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    probe_task_token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    probe_run_token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    max_wall_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)


class ModelBudgetCounter(Scoped, Base):
    __tablename__ = "model_budget_counters"
    __table_args__ = (
        UniqueConstraint("project_id", "workload", "scope", "scope_id", name="uq_budget_counter_workload_scope"),
        CheckConstraint("workload IN ('interactive', 'probe')", name="ck_budget_counter_workload"),
        CheckConstraint("scope IN ('project', 'task', 'run')", name="ck_budget_counter_scope"),
        CheckConstraint(
            "(scope = 'project' AND scope_id = project_id AND task_id IS NULL AND run_id IS NULL) OR "
            "(scope = 'task' AND task_id IS NOT NULL AND scope_id = task_id AND run_id IS NULL) OR "
            "(scope = 'run' AND task_id IS NOT NULL AND run_id IS NOT NULL AND scope_id = run_id)",
            name="ck_budget_counter_scope_identity",
        ),
        CheckConstraint("reserved_usd >= 0", name="ck_budget_reserved_usd_nonnegative"),
        CheckConstraint("billed_usd >= 0", name="ck_budget_billed_usd_nonnegative"),
        CheckConstraint("estimated_usd >= 0", name="ck_budget_estimated_usd_nonnegative"),
        CheckConstraint("reserved_tokens >= 0", name="ck_budget_reserved_tokens_nonnegative"),
        CheckConstraint("used_tokens >= 0", name="ck_budget_used_tokens_nonnegative"),
        CheckConstraint("used_steps >= 0", name="ck_budget_used_steps_nonnegative"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    workload: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[UUID] = mapped_column()
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    billed_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    estimated_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    used_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    used_steps: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class ModelReservation(Scoped, Base):
    __tablename__ = "model_reservations"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_hash", name="uq_model_reservation_idempotency"),
        CheckConstraint("status IN ('pending', 'settled', 'unknown', 'released')", name="ck_model_reservation_status"),
        CheckConstraint("reserved_usd > 0", name="ck_model_reservation_usd_positive"),
        CheckConstraint("reserved_tokens > 0", name="ck_model_reservation_tokens_positive"),
        Index("ix_model_reservations_stale", "status", "lease_expires_at"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    pool_id: Mapped[UUID] = mapped_column(ForeignKey("model_pools.id", ondelete="CASCADE"), index=True)
    deployment_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_deployments.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[UUID] = mapped_column(unique=True, nullable=False)
    idempotency_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    per_attempt_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    per_attempt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    workload: Mapped[str] = mapped_column(String(24), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelRequestLedger(Base):
    """Append-only event ledger. Prompt and response content are never stored here."""

    __tablename__ = "model_request_ledger"
    __table_args__ = (
        CheckConstraint("event_type IN ('reserved', 'attempt_started', 'attempt_failed', 'fallback_selected', 'attempt_succeeded', 'settled', 'stale_reconciled', 'usage_reconciled')", name="ck_model_ledger_event"),
        UniqueConstraint("project_id", "gateway_usage_source_observation_id", name="uq_model_ledger_gateway_usage_source"),
        Index("ix_model_ledger_project_created", "project_id", "created_at"),
        Index("ix_model_ledger_request_attempt", "request_id", "attempt"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("model_provider_accounts.id", ondelete="SET NULL"))
    deployment_id: Mapped[UUID | None] = mapped_column(ForeignKey("model_deployments.id", ondelete="SET NULL"))
    pool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    model_alias: Mapped[str] = mapped_column(String(120), nullable=False)
    actual_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_identifier: Mapped[str | None] = mapped_column(String(200), nullable=True)
    route_decision: Mapped[str] = mapped_column(String(200), nullable=False)
    price_revision: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    usage_known: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    billed_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    provider_reported_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    gateway_usage_source_observation_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    estimated_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GatewayUsageImport(Scoped, Base):
    """Durable, metadata-only receipt for late or unmatched gateway usage."""

    __tablename__ = "gateway_usage_imports"
    __table_args__ = (
        UniqueConstraint("project_id", "source_observation_id", name="uq_gateway_usage_source_observation"),
        CheckConstraint("status IN ('matched', 'retryable', 'ambiguous', 'failed')", name="ck_gateway_usage_import_status"),
        Index("ix_gateway_usage_retry", "status", "next_retry_at"),
    )

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    submitted_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_observation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    request_identifier: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    gateway_billed_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    provider_reported_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    usage_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="retryable")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ledger_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
