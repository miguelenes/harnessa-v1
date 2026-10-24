"""Add immutable skill artifact provenance and compatibility lifecycle."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "4a2f7c9d1e60"
down_revision = "92d4a1b8c6f0"
branch_labels = None
depends_on = None

json_doc = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.alter_column("harness_experiences", "source_task_id", nullable=True)
    op.alter_column("harness_experiences", "source_experiment_id", nullable=True)
    op.drop_constraint("ck_harness_experience_status", "harness_experiences", type_="check")
    op.create_check_constraint(
        "ck_harness_experience_status",
        "harness_experiences",
        "status IN ('candidate','active','rejected','superseded','stale')",
    )
    op.add_column(
        "harness_experiences",
        sa.Column("source_kind", sa.String(24), nullable=False, server_default="generated"),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("conflict_state", sa.String(16), nullable=False, server_default="clear"),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("author", sa.String(200), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("producer", sa.String(200), nullable=False, server_default="harnessa"),
    )
    op.add_column(
        "harness_experiences", sa.Column("source_author_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "harness_experiences",
        sa.Column("source_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("source_evaluation_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("scope_type", sa.String(16), nullable=False, server_default="repository"),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("scope_id", sa.String(300), nullable=False, server_default=""),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("license_id", sa.String(120), nullable=False, server_default="UNKNOWN"),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("dependencies", json_doc, nullable=False, server_default="[]"),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("allowed_tools", json_doc, nullable=False, server_default="[]"),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("trust_level", sa.String(16), nullable=False, server_default="untrusted"),
    )
    op.add_column("harness_experiences", sa.Column("artifact_path", sa.String(300), nullable=True))
    op.add_column("harness_experiences", sa.Column("artifact_digest", sa.String(64), nullable=True))
    op.add_column(
        "harness_experiences", sa.Column("artifact_storage_key", sa.String(300), nullable=True)
    )
    op.add_column(
        "harness_experiences", sa.Column("artifact_size_bytes", sa.Integer(), nullable=True)
    )
    op.add_column(
        "harness_experiences",
        sa.Column(
            "confidence_rationale",
            sa.Text(),
            nullable=False,
            server_default="Legacy experience; confidence is not established.",
        ),
    )
    op.add_column(
        "harness_experiences",
        sa.Column("compatibility_snapshot", json_doc, nullable=False, server_default="{}"),
    )
    op.add_column(
        "harness_experiences", sa.Column("stale_reason", json_doc, nullable=True)
    )
    op.add_column("harness_experiences", sa.Column("stale_at", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "fk_harness_experience_author",
        "harness_experiences",
        "users",
        ["source_author_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_harness_experience_source_run",
        "harness_experiences",
        "runs",
        ["source_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_harness_experience_source_evaluation",
        "harness_experiences",
        "evaluation_runs",
        ["source_evaluation_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_harness_experience_conflict_state",
        "harness_experiences",
        "conflict_state IN ('clear','conflict')",
    )
    op.create_check_constraint(
        "ck_harness_experience_source_kind",
        "harness_experiences",
        "source_kind IN ('generated','imported','operator_authored')",
    )
    op.create_check_constraint(
        "ck_harness_experience_scope_type",
        "harness_experiences",
        "scope_type IN ('task','repository','project','organization')",
    )
    op.create_check_constraint(
        "ck_harness_experience_trust_level",
        "harness_experiences",
        "trust_level IN ('untrusted','reviewed')",
    )
    op.create_check_constraint(
        "ck_harness_experience_artifact_size",
        "harness_experiences",
        "artifact_size_bytes IS NULL OR artifact_size_bytes >= 0",
    )
    op.create_index("ix_harness_experience_source_run_id", "harness_experiences", ["source_run_id"])
    op.create_index(
        "ix_harness_experience_source_evaluation_run_id",
        "harness_experiences",
        ["source_evaluation_run_id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION harnessa_reject_skill_content_update() RETURNS trigger AS $$
            BEGIN
              IF ROW(
                NEW.organization_id, NEW.project_id, NEW.source_task_id,
                NEW.source_experiment_id, NEW.source_run_id, NEW.source_evaluation_run_id,
                NEW.experience_key, NEW.version, NEW.repository_scope, NEW.domain_scope,
                NEW.cue, NEW.lesson, NEW.evidence_refs, NEW.candidate_digest, NEW.conflict_key,
                NEW.conflict_state,
                NEW.source_kind, NEW.author, NEW.producer, NEW.source_author_id,
                NEW.scope_type, NEW.scope_id, NEW.license_id, NEW.dependencies, NEW.allowed_tools,
                NEW.trust_level, NEW.artifact_path, NEW.artifact_digest, NEW.artifact_storage_key,
                NEW.artifact_size_bytes, NEW.confidence_rationale, NEW.compatibility_snapshot,
                NEW.expiry_at, NEW.policy_revision
              ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.project_id, OLD.source_task_id,
                OLD.source_experiment_id, OLD.source_run_id, OLD.source_evaluation_run_id,
                OLD.experience_key, OLD.version, OLD.repository_scope, OLD.domain_scope,
                OLD.cue, OLD.lesson, OLD.evidence_refs, OLD.candidate_digest, OLD.conflict_key,
                OLD.conflict_state,
                OLD.source_kind, OLD.author, OLD.producer, OLD.source_author_id,
                OLD.scope_type, OLD.scope_id, OLD.license_id, OLD.dependencies, OLD.allowed_tools,
                OLD.trust_level, OLD.artifact_path, OLD.artifact_digest, OLD.artifact_storage_key,
                OLD.artifact_size_bytes, OLD.confidence_rationale, OLD.compatibility_snapshot,
                OLD.expiry_at, OLD.policy_revision
              ) THEN
                RAISE EXCEPTION 'Harness experience content and provenance are immutable';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_harness_skill_content_immutable
            BEFORE UPDATE ON harness_experiences
            FOR EACH ROW EXECUTE FUNCTION harnessa_reject_skill_content_update();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER trg_harness_skill_content_immutable ON harness_experiences")
        op.execute("DROP FUNCTION harnessa_reject_skill_content_update()")
    op.drop_index("ix_harness_experience_source_evaluation_run_id", table_name="harness_experiences")
    op.drop_index("ix_harness_experience_source_run_id", table_name="harness_experiences")
    for name in (
        "ck_harness_experience_artifact_size",
        "ck_harness_experience_conflict_state",
        "ck_harness_experience_trust_level",
        "ck_harness_experience_scope_type",
        "ck_harness_experience_source_kind",
    ):
        op.drop_constraint(name, "harness_experiences", type_="check")
    for name in (
        "fk_harness_experience_source_evaluation",
        "fk_harness_experience_source_run",
        "fk_harness_experience_author",
    ):
        op.drop_constraint(name, "harness_experiences", type_="foreignkey")
    for name in (
        "stale_at", "stale_reason", "compatibility_snapshot", "confidence_rationale",
        "artifact_size_bytes", "artifact_storage_key", "artifact_digest", "artifact_path",
        "trust_level", "allowed_tools", "dependencies", "license_id", "scope_id",
        "scope_type", "source_evaluation_run_id", "source_run_id", "source_author_id",
        "producer", "author", "conflict_state", "source_kind",
    ):
        op.drop_column("harness_experiences", name)
    op.drop_constraint("ck_harness_experience_status", "harness_experiences", type_="check")
    op.create_check_constraint(
        "ck_harness_experience_status",
        "harness_experiences",
        "status IN ('candidate','active','rejected','superseded')",
    )
    op.alter_column("harness_experiences", "source_experiment_id", nullable=False)
    op.alter_column("harness_experiences", "source_task_id", nullable=False)
