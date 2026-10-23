"""GitHub App setup, repository opt-in, webhook inbox, and object provenance."""

from __future__ import annotations

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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from harnessa.db.base import Base, Scoped

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class GitHubAppConfiguration(Scoped, Base):
    """Deployment secret references and public identity for one GitHub App."""

    __tablename__ = "github_app_configurations"
    __table_args__ = (
        UniqueConstraint("organization_id", "app_id", name="uq_github_app_org_app"),
        UniqueConstraint("webhook_id", name="uq_github_app_webhook_id"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    app_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    webhook_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    private_key_secret_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    webhook_secret_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    connection_status: Mapped[str] = mapped_column(
        String(24), default="not_checked", nullable=False
    )
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class GitHubInstallation(Scoped, Base):
    """A verified installation; repository access is still opted into separately."""

    __tablename__ = "github_installations"
    __table_args__ = (
        UniqueConstraint("app_configuration_id", "installation_id", name="uq_github_installation"),
        CheckConstraint(
            "repository_selection IN ('all','selected','unknown')",
            name="ck_github_installation_selection",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    app_configuration_id: Mapped[UUID] = mapped_column(
        ForeignKey("github_app_configurations.id", ondelete="CASCADE"), index=True
    )
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    account_login: Mapped[str] = mapped_column(String(200), nullable=False)
    account_type: Mapped[str] = mapped_column(String(40), nullable=False)
    repository_selection: Mapped[str] = mapped_column(String(20), nullable=False)
    granted_permissions: Mapped[dict[str, str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class GitHubRepositoryLink(Scoped, Base):
    """Explicitly opted-in mapping between a Harnessa repo and a GitHub repo."""

    __tablename__ = "github_repository_links"
    __table_args__ = (
        UniqueConstraint("repository_id", name="uq_github_link_repository"),
        UniqueConstraint(
            "installation_id", "github_repository_id", name="uq_github_link_remote_repository"
        ),
        CheckConstraint(
            "setup_status IN ('ready','stale','revoked','error')",
            name="ck_github_link_setup_status",
        ),
        CheckConstraint(
            "archive_policy IN ('pre_merge','post_merge')",
            name="ck_github_link_archive_policy",
        ),
        Index("ix_github_links_project", "project_id", "opted_in"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("github_installations.id", ondelete="RESTRICT"), index=True
    )
    github_repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(401), nullable=False)
    opted_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    required_checks: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    topic_label: Mapped[str] = mapped_column(String(80), default="harnessa", nullable=False)
    archive_policy: Mapped[str] = mapped_column(String(20), default="pre_merge", nullable=False)
    setup_status: Mapped[str] = mapped_column(String(20), default="ready", nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issue_reconciliation_page: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    issue_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class GitHubActorMapping(Scoped, Base):
    """Maps a GitHub numeric actor ID to a local identity; local grants remain authoritative."""

    __tablename__ = "github_actor_mappings"
    __table_args__ = (
        UniqueConstraint("installation_id", "github_user_id", name="uq_github_actor_id"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("github_installations.id", ondelete="CASCADE"), index=True
    )
    github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    github_login: Mapped[str] = mapped_column(String(200), nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class GitHubWebhookDelivery(Scoped, Base):
    """Durable GitHub webhook inbox; the payload hash references its exact raw bytes."""

    __tablename__ = "github_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("app_configuration_id", "delivery_id", name="uq_github_delivery"),
        CheckConstraint(
            "status IN ('pending','processing','processed','ignored','failed')",
            name="ck_github_delivery_status",
        ),
        Index("ix_github_delivery_due", "status", "next_attempt_at", "created_at"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    app_configuration_id: Mapped[UUID] = mapped_column(
        ForeignKey("github_app_configurations.id", ondelete="CASCADE"), index=True
    )
    delivery_id: Mapped[str] = mapped_column(String(80), nullable=False)
    event_name: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str | None] = mapped_column(String(80), nullable=True)
    installation_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    github_repository_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_reference: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)


class GitHubWebhookOutbox(Scoped, Base):
    """Processing intent committed with the corresponding webhook inbox receipt."""

    __tablename__ = "github_webhook_outbox"
    __table_args__ = (
        UniqueConstraint("delivery_id", name="uq_github_webhook_outbox_delivery"),
        CheckConstraint(
            "status IN ('pending','processing','completed')", name="ck_github_webhook_outbox_status"
        ),
        Index("ix_github_webhook_outbox_due", "status", "next_attempt_at", "created_at"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("github_webhook_deliveries.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class GitHubSyncObject(Scoped, Base):
    """Correlation and reconciled remote state for issues, branches, PRs, and checks."""

    __tablename__ = "github_sync_objects"
    __table_args__ = (
        UniqueConstraint(
            "repository_link_id", "object_kind", "github_object_id", name="uq_github_object"
        ),
        UniqueConstraint("stable_key", name="uq_github_object_stable_key"),
        CheckConstraint(
            "object_kind IN ('issue','branch','pull_request','check_run')",
            name="ck_github_object_kind",
        ),
        CheckConstraint(
            "object_purpose IN ('review','maintenance_archive')",
            name="ck_github_object_purpose",
        ),
        Index("ix_github_sync_task", "project_id", "task_id", "object_kind"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    repository_link_id: Mapped[UUID] = mapped_column(
        ForeignKey("github_repository_links.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    object_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    object_purpose: Mapped[str] = mapped_column(String(32), default="review", nullable=False)
    github_object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    object_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stable_key: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    check_results: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    field_baseline: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    sync_conflicts: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
