"""Add durable Harness² experiments, evidence, experience and promotion records."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "52c0a42de71f"
down_revision = "8c7e2d1f4a90"
branch_labels = None
depends_on = None
json_doc = sa.JSON().with_variant(JSONB(), "postgresql")


def _scoped() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "harness_policies",
        *_scoped(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("max_k", sa.Integer(), nullable=False),
        sa.Column("max_probe_concurrency", sa.Integer(), nullable=False),
        sa.Column("reviewed_promotion", sa.Boolean(), nullable=False),
        sa.Column("cost_multiplier", sa.Numeric(6, 3), nullable=False),
        sa.Column("regression_gate_revision", sa.String(80), nullable=False),
        sa.Column("security_gate_revision", sa.String(80), nullable=False),
        sa.Column("evaluator_policy_revision", sa.String(80), nullable=False),
        sa.CheckConstraint("max_k BETWEEN 1 AND 3", name="ck_harness_policy_k"),
        sa.CheckConstraint(
            "max_probe_concurrency BETWEEN 1 AND 12", name="ck_harness_policy_concurrency"
        ),
        sa.CheckConstraint("reviewed_promotion IS TRUE", name="ck_harness_policy_reviewed"),
        sa.CheckConstraint("cost_multiplier >= 1", name="ck_harness_policy_cost_multiplier"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_harness_policy_project"),
    )
    op.create_index("ix_harness_policies_organization_id", "harness_policies", ["organization_id"])
    op.create_index("ix_harness_policies_project_id", "harness_policies", ["project_id"])

    op.create_table(
        "harness_experiments",
        *_scoped(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("stable_key", sa.String(200), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("specification_hash", sa.String(64), nullable=False),
        sa.Column("specification_revision", sa.Integer(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("harness2_revision", sa.String(40), nullable=False),
        sa.Column("baseline_digest", sa.String(64), nullable=True),
        sa.Column("result", json_doc, nullable=False),
        sa.Column("result_digest", sa.String(64), nullable=True),
        sa.Column("result_storage_key", sa.String(300), nullable=True),
        sa.Column("result_size_bytes", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('parallel','sequential','both')", name="ck_harness_experiment_mode"
        ),
        sa.CheckConstraint("k BETWEEN 1 AND 3", name="ck_harness_experiment_k"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','blocked')",
            name="ck_harness_experiment_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_harness_experiment_attempts"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "stable_key", name="uq_harness_experiment_key"),
    )
    op.create_index(
        "ix_harness_experiments_organization_id", "harness_experiments", ["organization_id"]
    )
    op.create_index("ix_harness_experiments_project_id", "harness_experiments", ["project_id"])
    op.create_index("ix_harness_experiments_task_id", "harness_experiments", ["task_id"])
    op.create_index("ix_harness_experiment_work", "harness_experiments", ["status", "created_at"])

    op.create_table(
        "harness_probe_runs",
        *_scoped(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=True),
        sa.Column("wall_ms", sa.Integer(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("route_alias", sa.String(120), nullable=True),
        sa.Column("image_revision", sa.String(300), nullable=True),
        sa.Column("tool_revision", sa.String(120), nullable=True),
        sa.Column("harness_digest", sa.String(64), nullable=True),
        sa.Column("trace_digest", sa.String(64), nullable=True),
        sa.Column("trace_storage_key", sa.String(300), nullable=True),
        sa.Column("trace_size_bytes", sa.Integer(), nullable=True),
        sa.Column("component_diff", json_doc, nullable=False),
        sa.Column("outcome_detail", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "phase IN ('baseline','candidate','composed')", name="ck_harness_probe_phase"
        ),
        sa.CheckConstraint(
            "status IN ('succeeded','failed','cancelled','unknown')", name="ck_harness_probe_status"
        ),
        sa.CheckConstraint(
            "wall_ms IS NULL OR wall_ms >= 0", name="ck_harness_probe_wall_nonnegative"
        ),
        sa.CheckConstraint(
            "tokens IS NULL OR tokens >= 0", name="ck_harness_probe_tokens_nonnegative"
        ),
        sa.CheckConstraint(
            "cost_usd IS NULL OR cost_usd >= 0", name="ck_harness_probe_cost_nonnegative"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["experiment_id"], ["harness_experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "label", name="uq_harness_probe_label"),
    )
    op.create_index(
        "ix_harness_probe_runs_organization_id", "harness_probe_runs", ["organization_id"]
    )
    op.create_index("ix_harness_probe_runs_project_id", "harness_probe_runs", ["project_id"])
    op.create_index("ix_harness_probe_runs_task_id", "harness_probe_runs", ["task_id"])
    op.create_index("ix_harness_probe_runs_experiment_id", "harness_probe_runs", ["experiment_id"])

    op.create_table(
        "harness_experiences",
        *_scoped(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_task_id", sa.Uuid(), nullable=False),
        sa.Column("source_experiment_id", sa.Uuid(), nullable=False),
        sa.Column("experience_key", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("repository_scope", sa.String(200), nullable=False),
        sa.Column("domain_scope", sa.String(120), nullable=False),
        sa.Column("cue", sa.String(1000), nullable=False),
        sa.Column("lesson", sa.String(2000), nullable=False),
        sa.Column("evidence_refs", json_doc, nullable=False),
        sa.Column("candidate_digest", sa.String(64), nullable=False),
        sa.Column("conflict_key", sa.String(120), nullable=False),
        sa.Column("expiry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_harness_experience_version"),
        sa.CheckConstraint(
            "status IN ('candidate','active','rejected','superseded')",
            name="ck_harness_experience_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_experiment_id"], ["harness_experiments.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "experience_key", "version", name="uq_harness_experience_version"
        ),
    )
    op.create_index(
        "ix_harness_experiences_organization_id", "harness_experiences", ["organization_id"]
    )
    op.create_index("ix_harness_experiences_project_id", "harness_experiences", ["project_id"])
    op.create_index(
        "ix_harness_experiences_source_task_id", "harness_experiences", ["source_task_id"]
    )
    op.create_index(
        "ix_harness_experiences_source_experiment_id",
        "harness_experiences",
        ["source_experiment_id"],
    )
    op.create_index(
        "ix_harness_experience_scope",
        "harness_experiences",
        ["project_id", "repository_scope", "status"],
    )

    op.create_table(
        "harness_promotions",
        *_scoped(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("experience_id", sa.Uuid(), nullable=False),
        sa.Column("previous_experience_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("stable_key", sa.String(200), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("gate_evidence", json_doc, nullable=False),
        sa.CheckConstraint("action IN ('promote','rollback')", name="ck_harness_promotion_action"),
        sa.CheckConstraint(
            "decision IN ('approved','rejected')", name="ck_harness_promotion_decision"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["experience_id"], ["harness_experiences.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["previous_experience_id"], ["harness_experiences.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "stable_key", name="uq_harness_promotion_key"),
    )
    op.create_index(
        "ix_harness_promotions_organization_id", "harness_promotions", ["organization_id"]
    )
    op.create_index("ix_harness_promotions_project_id", "harness_promotions", ["project_id"])
    op.create_index("ix_harness_promotions_experience_id", "harness_promotions", ["experience_id"])


def downgrade() -> None:
    op.drop_table("harness_promotions")
    op.drop_table("harness_experiences")
    op.drop_table("harness_probe_runs")
    op.drop_table("harness_experiments")
    op.drop_table("harness_policies")
