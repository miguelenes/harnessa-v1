"""GitHub App configuration, durable webhook inbox, and sync provenance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a10b2f6d9c41"
down_revision = "3e8a91d72bc4"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.add_column(
        "effect_intents",
        sa.Column("result", JSON_DOCUMENT, server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column("review_decisions", sa.Column("github_head_sha", sa.String(64), nullable=True))

    op.create_table(
        "github_app_configurations",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.BigInteger(), nullable=False),
        sa.Column("webhook_id", sa.BigInteger(), nullable=False),
        sa.Column("private_key_secret_ref", sa.String(240), nullable=False),
        sa.Column("webhook_secret_ref", sa.String(240), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("connection_status", sa.String(24), server_default="not_checked", nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "app_id", name="uq_github_app_org_app"),
        sa.UniqueConstraint("webhook_id", name="uq_github_app_webhook_id"),
    )
    op.create_index(
        "ix_github_app_configurations_organization_id",
        "github_app_configurations",
        ["organization_id"],
    )

    op.create_table(
        "github_installations",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("app_configuration_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("account_login", sa.String(200), nullable=False),
        sa.Column("account_type", sa.String(40), nullable=False),
        sa.Column("repository_selection", sa.String(20), nullable=False),
        sa.Column("granted_permissions", JSON_DOCUMENT, nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "repository_selection IN ('all','selected','unknown')",
            name="ck_github_installation_selection",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["app_configuration_id"], ["github_app_configurations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "app_configuration_id", "installation_id", name="uq_github_installation"
        ),
    )
    op.create_index(
        "ix_github_installations_app_configuration_id",
        "github_installations",
        ["app_configuration_id"],
    )

    op.create_table(
        "github_repository_links",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("github_repository_id", sa.BigInteger(), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("full_name", sa.String(401), nullable=False),
        sa.Column("opted_in", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("authorized_by", sa.Uuid(), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("required_checks", JSON_DOCUMENT, nullable=False),
        sa.Column("topic_label", sa.String(80), server_default="harnessa", nullable=False),
        sa.Column("setup_status", sa.String(20), server_default="ready", nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "setup_status IN ('ready','stale','revoked','error')",
            name="ck_github_link_setup_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["installation_id"], ["github_installations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["authorized_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", name="uq_github_link_repository"),
        sa.UniqueConstraint(
            "installation_id", "github_repository_id", name="uq_github_link_remote_repository"
        ),
    )
    op.create_index(
        "ix_github_repository_links_project_id", "github_repository_links", ["project_id"]
    )
    op.create_index(
        "ix_github_repository_links_installation_id", "github_repository_links", ["installation_id"]
    )
    op.create_index(
        "ix_github_links_project", "github_repository_links", ["project_id", "opted_in"]
    )

    op.create_table(
        "github_actor_mappings",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(200), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["installation_id"], ["github_installations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("installation_id", "github_user_id", name="uq_github_actor_id"),
    )
    op.create_index(
        "ix_github_actor_mappings_installation_id", "github_actor_mappings", ["installation_id"]
    )
    op.create_index("ix_github_actor_mappings_user_id", "github_actor_mappings", ["user_id"])

    op.create_table(
        "github_webhook_deliveries",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("app_configuration_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.String(80), nullable=False),
        sa.Column("event_name", sa.String(80), nullable=False),
        sa.Column("action", sa.String(80), nullable=True),
        sa.Column("installation_number", sa.BigInteger(), nullable=True),
        sa.Column("github_repository_id", sa.BigInteger(), nullable=True),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_reference", sa.String(80), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("result", JSON_DOCUMENT, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending','processing','processed','ignored','failed')",
            name="ck_github_delivery_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["app_configuration_id"], ["github_app_configurations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_configuration_id", "delivery_id", name="uq_github_delivery"),
    )
    op.create_index(
        "ix_github_webhook_deliveries_app_configuration_id",
        "github_webhook_deliveries",
        ["app_configuration_id"],
    )
    op.create_index(
        "ix_github_delivery_due",
        "github_webhook_deliveries",
        ["status", "next_attempt_at", "created_at"],
    )

    op.create_table(
        "github_webhook_outbox",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed')",
            name="ck_github_webhook_outbox_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["delivery_id"], ["github_webhook_deliveries.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id", name="uq_github_webhook_outbox_delivery"),
    )
    op.create_index(
        "ix_github_webhook_outbox_delivery_id", "github_webhook_outbox", ["delivery_id"]
    )
    op.create_index(
        "ix_github_webhook_outbox_due",
        "github_webhook_outbox",
        ["status", "next_attempt_at", "created_at"],
    )

    op.create_table(
        "github_sync_objects",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repository_link_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("object_kind", sa.String(24), nullable=False),
        sa.Column("github_object_id", sa.String(128), nullable=False),
        sa.Column("object_number", sa.Integer(), nullable=True),
        sa.Column("stable_key", sa.String(200), nullable=False),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("check_results", JSON_DOCUMENT, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("field_baseline", JSON_DOCUMENT, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("sync_conflicts", JSON_DOCUMENT, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "object_kind IN ('issue','branch','pull_request','check_run')",
            name="ck_github_object_kind",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["repository_link_id"], ["github_repository_links.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repository_link_id", "object_kind", "github_object_id", name="uq_github_object"
        ),
        sa.UniqueConstraint("stable_key", name="uq_github_object_stable_key"),
    )
    op.create_index("ix_github_sync_objects_project_id", "github_sync_objects", ["project_id"])
    op.create_index(
        "ix_github_sync_objects_repository_link_id", "github_sync_objects", ["repository_link_id"]
    )
    op.create_index("ix_github_sync_objects_task_id", "github_sync_objects", ["task_id"])
    op.create_index(
        "ix_github_sync_task", "github_sync_objects", ["project_id", "task_id", "object_kind"]
    )


def downgrade() -> None:
    op.drop_index("ix_github_sync_task", table_name="github_sync_objects")
    op.drop_index("ix_github_sync_objects_task_id", table_name="github_sync_objects")
    op.drop_index("ix_github_sync_objects_repository_link_id", table_name="github_sync_objects")
    op.drop_index("ix_github_sync_objects_project_id", table_name="github_sync_objects")
    op.drop_table("github_sync_objects")
    op.drop_index("ix_github_webhook_outbox_due", table_name="github_webhook_outbox")
    op.drop_index("ix_github_webhook_outbox_delivery_id", table_name="github_webhook_outbox")
    op.drop_table("github_webhook_outbox")
    op.drop_index("ix_github_delivery_due", table_name="github_webhook_deliveries")
    op.drop_index(
        "ix_github_webhook_deliveries_app_configuration_id", table_name="github_webhook_deliveries"
    )
    op.drop_table("github_webhook_deliveries")
    op.drop_index("ix_github_actor_mappings_user_id", table_name="github_actor_mappings")
    op.drop_index("ix_github_actor_mappings_installation_id", table_name="github_actor_mappings")
    op.drop_table("github_actor_mappings")
    op.drop_index("ix_github_repository_links_project_id", table_name="github_repository_links")
    op.drop_index(
        "ix_github_repository_links_installation_id", table_name="github_repository_links"
    )
    op.drop_index("ix_github_links_project", table_name="github_repository_links")
    op.drop_table("github_repository_links")
    op.drop_index("ix_github_installations_app_configuration_id", table_name="github_installations")
    op.drop_table("github_installations")
    op.drop_index(
        "ix_github_app_configurations_organization_id", table_name="github_app_configurations"
    )
    op.drop_table("github_app_configurations")
    op.drop_column("review_decisions", "github_head_sha")
    op.drop_column("effect_intents", "result")
