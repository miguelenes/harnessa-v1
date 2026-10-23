"""approved verification policy and immutable review/integration lifecycle"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "3e8a91d72bc4"
down_revision = "c83b2e441a79"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "verification_manifest_revisions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.String(500), nullable=False),
        sa.Column("source_revision", sa.String(64), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("manifest", JSON_DOCUMENT, nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column(
            "protected_fixtures", JSON_DOCUMENT, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("protected_fixture_digest", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint("revision > 0", name="ck_verification_manifest_revision_positive"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "revision", name="uq_verification_manifest_revision"),
    )
    op.create_index(
        "ix_verification_manifests_latest",
        "verification_manifest_revisions",
        ["repository_id", "revision"],
    )

    op.create_table(
        "verification_executions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_revision_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("source_revision", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("result", JSON_DOCUMENT, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "attempt > 0 AND fencing_token > 0", name="ck_verification_execution_fence"
        ),
        sa.CheckConstraint(
            "outcome IN ('passed','failed','manual_review_required','unverified')",
            name="ck_verification_execution_outcome",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["manifest_revision_id"], ["verification_manifest_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "fencing_token",
            "manifest_revision_id",
            name="uq_verification_execution_identity",
        ),
    )
    op.create_index(
        "ix_verification_execution_task",
        "verification_executions",
        ["project_id", "task_id", "created_at"],
    )

    op.create_table(
        "review_bundles",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("verification_execution_id", sa.Uuid(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("specification_hash", sa.String(64), nullable=False),
        sa.Column("specification_revision", sa.Integer(), nullable=False),
        sa.Column("harness_revision", sa.String(120), nullable=True),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("bundle_digest", sa.String(64), nullable=False),
        sa.Column("artifact_digest", sa.String(64), nullable=False),
        sa.Column("bundle", JSON_DOCUMENT, nullable=False),
        sa.Column("status", sa.String(20), server_default="prepared", nullable=False),
        sa.Column("invalidated_reason", sa.String(300), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint("fencing_token > 0", name="ck_review_bundle_fence_positive"),
        sa.CheckConstraint(
            "status IN ('prepared','published','invalidated')", name="ck_review_bundle_status"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["verification_execution_id"], ["verification_executions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "bundle_digest", name="uq_review_bundle_digest"),
        sa.UniqueConstraint("run_id", "fencing_token", name="uq_review_bundle_run_fence"),
    )
    op.create_index(
        "ix_review_bundles_task", "review_bundles", ["project_id", "task_id", "created_at"]
    )

    op.create_table(
        "review_decisions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("bundle_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("bundle_digest", sa.String(64), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "decision IN ('approve','request_changes','comment')", name="ck_review_decision_kind"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bundle_id"], ["review_bundles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "idempotency_key", name="uq_review_decision_idempotency"),
    )

    op.create_table(
        "integration_evidence",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("bundle_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("stable_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("integration_revision", sa.String(200), nullable=True),
        sa.Column("confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "mode IN ('github','local_manual')", name="ck_integration_evidence_mode"
        ),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','unknown','failed')",
            name="ck_integration_evidence_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bundle_id"], ["review_bundles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "stable_key", name="uq_integration_evidence_key"),
    )

    op.create_table(
        "post_integration_steps",
        sa.Column("integration_evidence_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result", JSON_DOCUMENT, nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "kind IN ('archive','learning','cleanup')", name="ck_post_integration_step_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed')",
            name="ck_post_integration_step_status",
        ),
        sa.ForeignKeyConstraint(
            ["integration_evidence_id"], ["integration_evidence.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("integration_evidence_id", "kind", name="uq_post_integration_step"),
    )

    op.add_column("runs", sa.Column("base_sha", sa.String(64), nullable=True))
    op.add_column("runs", sa.Column("specification_hash", sa.String(64), nullable=True))
    op.add_column("runs", sa.Column("specification_revision", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("specification_commit_sha", sa.String(64), nullable=True))
    op.add_column("runs", sa.Column("policy_revision", sa.String(80), nullable=True))
    op.add_column("runs", sa.Column("budget_policy_digest", sa.String(64), nullable=True))
    op.add_column("runs", sa.Column("harness_revision", sa.String(120), nullable=True))
    op.add_column("runs", sa.Column("harness_digest", sa.String(64), nullable=True))
    op.add_column("runs", sa.Column("agent_image", sa.String(300), nullable=True))
    op.add_column("runs", sa.Column("verification_manifest_revision_id", sa.Uuid(), nullable=True))
    op.add_column("runs", sa.Column("execution_result", JSON_DOCUMENT, nullable=True))
    op.create_foreign_key(
        "fk_runs_verification_manifest_revision",
        "runs",
        "verification_manifest_revisions",
        ["verification_manifest_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_runs_verification_manifest_revision", "runs", type_="foreignkey")
    op.drop_column("runs", "execution_result")
    op.drop_column("runs", "verification_manifest_revision_id")
    op.drop_column("runs", "agent_image")
    op.drop_column("runs", "harness_revision")
    op.drop_column("runs", "harness_digest")
    op.drop_column("runs", "budget_policy_digest")
    op.drop_column("runs", "policy_revision")
    op.drop_column("runs", "specification_revision")
    op.drop_column("runs", "specification_commit_sha")
    op.drop_column("runs", "specification_hash")
    op.drop_column("runs", "base_sha")
    op.drop_table("post_integration_steps")
    op.drop_table("integration_evidence")
    op.drop_table("review_decisions")
    op.drop_index("ix_review_bundles_task", table_name="review_bundles")
    op.drop_table("review_bundles")
    op.drop_index("ix_verification_execution_task", table_name="verification_executions")
    op.drop_table("verification_executions")
    op.drop_index("ix_verification_manifests_latest", table_name="verification_manifest_revisions")
    op.drop_table("verification_manifest_revisions")
