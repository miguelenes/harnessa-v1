"""Enforce grouped evaluation spend across child model runs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1f73498a2bc"
down_revision = "b172c093ef51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_budget_counters", sa.Column("evaluation_run_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_model_budget_counters_evaluation_run_id",
        "model_budget_counters",
        ["evaluation_run_id"],
    )
    op.create_foreign_key(
        "fk_model_budget_counters_evaluation_run_id_evaluation_runs",
        "model_budget_counters",
        "evaluation_runs",
        ["evaluation_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("ck_budget_counter_scope", "model_budget_counters", type_="check")
    op.drop_constraint(
        "ck_budget_counter_scope_identity", "model_budget_counters", type_="check"
    )
    op.create_check_constraint(
        "ck_budget_counter_scope",
        "model_budget_counters",
        "scope IN ('project', 'task', 'run', 'evaluation')",
    )
    op.create_check_constraint(
        "ck_budget_counter_scope_identity",
        "model_budget_counters",
        "(scope = 'project' AND scope_id = project_id AND task_id IS NULL AND run_id IS NULL AND evaluation_run_id IS NULL) OR "
        "(scope = 'task' AND task_id IS NOT NULL AND scope_id = task_id AND run_id IS NULL AND evaluation_run_id IS NULL) OR "
        "(scope = 'run' AND task_id IS NOT NULL AND run_id IS NOT NULL AND evaluation_run_id IS NULL AND scope_id = run_id) OR "
        "(scope = 'evaluation' AND task_id IS NOT NULL AND run_id IS NULL AND evaluation_run_id IS NOT NULL AND scope_id = evaluation_run_id)",
    )

    op.add_column("model_reservations", sa.Column("evaluation_run_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_model_reservations_evaluation_run_id",
        "model_reservations",
        ["evaluation_run_id"],
    )
    op.create_foreign_key(
        "fk_model_reservations_evaluation_run_id_evaluation_runs",
        "model_reservations",
        "evaluation_runs",
        ["evaluation_run_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_model_reservations_evaluation_run_id_evaluation_runs",
        "model_reservations",
        type_="foreignkey",
    )
    op.drop_index("ix_model_reservations_evaluation_run_id", table_name="model_reservations")
    op.drop_column("model_reservations", "evaluation_run_id")

    # Evaluation counters duplicate the same call charges already retained at project,
    # task, and execution-run scopes; reservations and ledger rows remain the audit record.
    op.execute(
        "DELETE FROM model_budget_counters WHERE scope = 'evaluation'"
    )
    op.drop_constraint("ck_budget_counter_scope_identity", "model_budget_counters", type_="check")
    op.drop_constraint("ck_budget_counter_scope", "model_budget_counters", type_="check")
    op.create_check_constraint(
        "ck_budget_counter_scope",
        "model_budget_counters",
        "scope IN ('project', 'task', 'run')",
    )
    op.create_check_constraint(
        "ck_budget_counter_scope_identity",
        "model_budget_counters",
        "(scope = 'project' AND scope_id = project_id AND task_id IS NULL AND run_id IS NULL) OR "
        "(scope = 'task' AND task_id IS NOT NULL AND scope_id = task_id AND run_id IS NULL) OR "
        "(scope = 'run' AND task_id IS NOT NULL AND run_id IS NOT NULL AND scope_id = run_id)",
    )
    op.drop_constraint(
        "fk_model_budget_counters_evaluation_run_id_evaluation_runs",
        "model_budget_counters",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_model_budget_counters_evaluation_run_id", table_name="model_budget_counters"
    )
    op.drop_column("model_budget_counters", "evaluation_run_id")
