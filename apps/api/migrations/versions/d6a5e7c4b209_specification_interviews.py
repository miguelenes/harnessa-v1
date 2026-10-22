"""durable OpenSpec interviews, answers, revisions, and readiness"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d6a5e7c4b209"
down_revision = "5c0a4b6d1e9f"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def _scoped_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "specification_interviews",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("change_id", sa.String(length=80), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("artifact_revision", sa.Integer(), nullable=False),
        sa.Column("artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_revision", sa.String(length=80), nullable=False),
        sa.Column("readiness_state", sa.String(length=20), nullable=False),
        sa.Column("readiness_snapshot", JSON_DOCUMENT, nullable=True),
        sa.Column("architect_idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("architect_source_revision", sa.Integer(), nullable=True),
        sa.Column("architect_artifact_revision", sa.Integer(), nullable=True),
        *_scoped_columns(),
        sa.CheckConstraint("revision > 0", name="ck_spec_interview_revision_positive"),
        sa.CheckConstraint(
            "artifact_revision > 0", name="ck_spec_artifact_revision_positive"
        ),
        sa.CheckConstraint(
            "readiness_state IN ('NOT_EVALUATED','NOT_READY','READY','STALE')",
            name="ck_spec_interview_readiness_state",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_spec_interviews_task"),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_spec_interviews_key"),
        sa.UniqueConstraint(
            "repository_id", "change_id", name="uq_spec_interviews_repository_change"
        ),
    )
    for column in ("organization_id", "project_id", "task_id", "repository_id"):
        op.create_index(
            f"ix_specification_interviews_{column}",
            "specification_interviews",
            [column],
        )

    op.create_table(
        "specification_questions",
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("proposed_assumption", sa.Text(), nullable=True),
        sa.Column("recommended_options", JSON_DOCUMENT, nullable=False),
        sa.Column("affected_artifacts", JSON_DOCUMENT, nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        *_scoped_columns(),
        sa.CheckConstraint(
            "classification IN ('blocking','nonblocking')",
            name="ck_spec_question_classification",
        ),
        sa.CheckConstraint(
            "status IN ('open','answered','auto_resolved','stale')",
            name="ck_spec_question_status",
        ),
        sa.CheckConstraint("attempt IS NULL OR attempt > 0", name="ck_spec_question_attempt_positive"),
        sa.ForeignKeyConstraint(
            ["interview_id"], ["specification_interviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("interview_id", "task_id", "run_id"):
        op.create_index(f"ix_specification_questions_{column}", "specification_questions", [column])

    op.create_table(
        "specification_answers",
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("answer_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("provenance", JSON_DOCUMENT, nullable=False),
        *_scoped_columns(),
        sa.ForeignKeyConstraint(
            ["interview_id"], ["specification_interviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["question_id"], ["specification_questions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("interview_id", "idempotency_key", name="uq_spec_answer_key"),
        sa.UniqueConstraint("question_id", name="uq_spec_answer_question"),
    )
    op.create_index("ix_specification_answers_interview_id", "specification_answers", ["interview_id"])
    op.create_index("ix_specification_answers_question_id", "specification_answers", ["question_id"])

    op.create_table(
        "specification_artifact_revisions",
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("paths", JSON_DOCUMENT, nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        *_scoped_columns(),
        sa.ForeignKeyConstraint(
            ["interview_id"], ["specification_interviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("interview_id", "revision", name="uq_spec_artifact_revision"),
        sa.UniqueConstraint("interview_id", "commit_sha", name="uq_spec_artifact_commit"),
    )
    op.create_index(
        "ix_specification_artifact_revisions_interview_id",
        "specification_artifact_revisions",
        ["interview_id"],
    )

    op.create_table(
        "specification_readiness_evaluations",
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("interview_revision", sa.Integer(), nullable=False),
        sa.Column("artifact_revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=False),
        sa.Column("policy_revision", sa.String(length=80), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("gates", JSON_DOCUMENT, nullable=False),
        sa.Column("evaluated_by", sa.Uuid(), nullable=False),
        sa.Column("signed_off_by", sa.Uuid(), nullable=True),
        *_scoped_columns(),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_spec_readiness_score_range"),
        sa.CheckConstraint(
            "state IN ('NOT_READY','READY','STALE')", name="ck_spec_readiness_state"
        ),
        sa.ForeignKeyConstraint(
            ["interview_id"], ["specification_interviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["evaluated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["signed_off_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_specification_readiness_evaluations_interview_id",
        "specification_readiness_evaluations",
        ["interview_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_specification_readiness_evaluations_interview_id",
        table_name="specification_readiness_evaluations",
    )
    op.drop_table("specification_readiness_evaluations")
    op.drop_index(
        "ix_specification_artifact_revisions_interview_id",
        table_name="specification_artifact_revisions",
    )
    op.drop_table("specification_artifact_revisions")
    op.drop_index("ix_specification_answers_question_id", table_name="specification_answers")
    op.drop_index("ix_specification_answers_interview_id", table_name="specification_answers")
    op.drop_table("specification_answers")
    op.drop_index("ix_specification_questions_run_id", table_name="specification_questions")
    op.drop_index("ix_specification_questions_task_id", table_name="specification_questions")
    op.drop_index("ix_specification_questions_interview_id", table_name="specification_questions")
    op.drop_table("specification_questions")
    op.drop_index(
        "ix_specification_interviews_repository_id", table_name="specification_interviews"
    )
    op.drop_index("ix_specification_interviews_task_id", table_name="specification_interviews")
    op.drop_index("ix_specification_interviews_project_id", table_name="specification_interviews")
    op.drop_index(
        "ix_specification_interviews_organization_id", table_name="specification_interviews"
    )
    op.drop_table("specification_interviews")
