"""fencing-bound local run artifacts"""

import sqlalchemy as sa
from alembic import op

revision = "5c0a4b6d1e9f"
down_revision = "b11f10974e82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("event_sequence", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.execute(
        """
        UPDATE tasks AS task
        SET event_sequence = GREATEST(
            task.version,
            COALESCE(
                (SELECT MAX(event.aggregate_sequence)
                 FROM domain_events AS event WHERE event.task_id = task.id),
                0
            )
        )
        """
    )
    op.alter_column("tasks", "event_sequence", server_default=None)
    op.create_table(
        "run_artifacts",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=300), nullable=False),
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
        sa.CheckConstraint("fencing_token > 0", name="ck_run_artifacts_fence_positive"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_run_artifacts_size_nonnegative"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "fencing_token", "digest", name="uq_run_artifact_fence_digest"
        ),
    )
    op.create_index("ix_run_artifacts_scope", "run_artifacts", ["project_id", "task_id", "run_id"])
    op.create_index(op.f("ix_run_artifacts_organization_id"), "run_artifacts", ["organization_id"])
    op.create_index(op.f("ix_run_artifacts_project_id"), "run_artifacts", ["project_id"])
    op.create_index(op.f("ix_run_artifacts_task_id"), "run_artifacts", ["task_id"])
    op.create_index(op.f("ix_run_artifacts_run_id"), "run_artifacts", ["run_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_run_artifacts_run_id"), table_name="run_artifacts")
    op.drop_index(op.f("ix_run_artifacts_task_id"), table_name="run_artifacts")
    op.drop_index(op.f("ix_run_artifacts_project_id"), table_name="run_artifacts")
    op.drop_index(op.f("ix_run_artifacts_organization_id"), table_name="run_artifacts")
    op.drop_index("ix_run_artifacts_scope", table_name="run_artifacts")
    op.drop_table("run_artifacts")
    op.drop_column("tasks", "event_sequence")
