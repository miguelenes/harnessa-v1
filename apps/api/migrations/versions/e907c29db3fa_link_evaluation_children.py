"""Link grouped evaluation children directly to their aggregate run."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e907c29db3fa"
down_revision = "d1f73498a2bc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harness_experiments",
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_harness_experiments_evaluation_run_id",
        "harness_experiments",
        ["evaluation_run_id"],
    )
    op.create_foreign_key(
        "fk_harness_experiments_evaluation_run_id_evaluation_runs",
        "harness_experiments",
        "evaluation_runs",
        ["evaluation_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Backfill using the existing immutable child-ID manifest. PostgreSQL JSONB
    # containment gives each child a direct indexed owner for future reservations.
    op.execute(sa.text("""
        UPDATE harness_experiments AS experiment
        SET evaluation_run_id = evaluation.id
        FROM evaluation_runs AS evaluation
        WHERE evaluation.project_id = experiment.project_id
          AND evaluation.experiment_ids @> jsonb_build_array(experiment.id::text)
    """))


def downgrade() -> None:
    op.drop_constraint(
        "fk_harness_experiments_evaluation_run_id_evaluation_runs",
        "harness_experiments",
        type_="foreignkey",
    )
    op.drop_index("ix_harness_experiments_evaluation_run_id", table_name="harness_experiments")
    op.drop_column("harness_experiments", "evaluation_run_id")
