"""Separate interactive and probe budget counters and ceilings."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8a6f2c1e9d40"
down_revision = "7d4a13b2e6f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, type_ in (
        ("probe_project_limit_usd", sa.Numeric(18, 8)),
        ("probe_task_limit_usd", sa.Numeric(18, 8)),
        ("probe_run_limit_usd", sa.Numeric(18, 8)),
        ("probe_project_token_limit", sa.BigInteger()),
        ("probe_task_token_limit", sa.BigInteger()),
        ("probe_run_token_limit", sa.BigInteger()),
    ):
        op.add_column("model_budget_policies", sa.Column(name, type_, nullable=True))
    op.execute(sa.text("""
        UPDATE model_budget_policies SET
            probe_project_limit_usd = project_limit_usd,
            probe_task_limit_usd = task_limit_usd,
            probe_run_limit_usd = run_limit_usd,
            probe_project_token_limit = project_token_limit,
            probe_task_token_limit = task_token_limit,
            probe_run_token_limit = run_token_limit
    """))
    for name in (
        "probe_project_limit_usd", "probe_task_limit_usd", "probe_run_limit_usd",
        "probe_project_token_limit", "probe_task_token_limit", "probe_run_token_limit",
    ):
        op.alter_column("model_budget_policies", name, nullable=False)
    for name, expression in (
        ("ck_budget_probe_project_limit_positive", "probe_project_limit_usd > 0"),
        ("ck_budget_probe_task_limit_positive", "probe_task_limit_usd > 0"),
        ("ck_budget_probe_run_limit_positive", "probe_run_limit_usd > 0"),
        ("ck_budget_probe_project_tokens_positive", "probe_project_token_limit > 0"),
        ("ck_budget_probe_task_tokens_positive", "probe_task_token_limit > 0"),
        ("ck_budget_probe_run_tokens_positive", "probe_run_token_limit > 0"),
    ):
        op.create_check_constraint(name, "model_budget_policies", expression)

    op.add_column("model_budget_counters", sa.Column("workload", sa.String(16), nullable=True))
    op.execute("UPDATE model_budget_counters SET workload = 'interactive'")
    op.alter_column("model_budget_counters", "workload", nullable=False)
    op.drop_constraint("uq_budget_counter_scope", "model_budget_counters", type_="unique")
    op.create_unique_constraint(
        "uq_budget_counter_workload_scope", "model_budget_counters",
        ["project_id", "workload", "scope", "scope_id"],
    )
    op.create_check_constraint(
        "ck_budget_counter_workload", "model_budget_counters",
        "workload IN ('interactive', 'probe')",
    )
    # Historical counters were shared across pools. Copying their full usage to both
    # workloads is conservative and prevents a migration from creating fresh capacity.
    op.execute(sa.text("""
        INSERT INTO model_budget_counters (
            id, created_at, updated_at, organization_id, project_id, task_id, run_id,
            scope, scope_id, workload, reserved_usd, billed_usd, estimated_usd,
            reserved_tokens, used_tokens, used_steps
        ) SELECT gen_random_uuid(), created_at, updated_at, organization_id, project_id,
            task_id, run_id, scope, scope_id, 'probe', reserved_usd, billed_usd,
            estimated_usd, reserved_tokens, used_tokens, used_steps
        FROM model_budget_counters WHERE workload = 'interactive'
    """))


def downgrade() -> None:
    # Preserve all recorded use when collapsing the two pools back into shared counters.
    op.execute(sa.text("""
        UPDATE model_budget_counters AS primary_counter SET
            reserved_usd = primary_counter.reserved_usd + probe_counter.reserved_usd,
            billed_usd = primary_counter.billed_usd + probe_counter.billed_usd,
            estimated_usd = primary_counter.estimated_usd + probe_counter.estimated_usd,
            reserved_tokens = primary_counter.reserved_tokens + probe_counter.reserved_tokens,
            used_tokens = primary_counter.used_tokens + probe_counter.used_tokens,
            used_steps = primary_counter.used_steps + probe_counter.used_steps
        FROM model_budget_counters AS probe_counter
        WHERE primary_counter.project_id = probe_counter.project_id
            AND primary_counter.scope = probe_counter.scope
            AND primary_counter.scope_id = probe_counter.scope_id
            AND primary_counter.workload = 'interactive' AND probe_counter.workload = 'probe'
    """))
    op.execute("DELETE FROM model_budget_counters WHERE workload = 'probe'")
    op.drop_constraint("ck_budget_counter_workload", "model_budget_counters", type_="check")
    op.drop_constraint("uq_budget_counter_workload_scope", "model_budget_counters", type_="unique")
    op.drop_column("model_budget_counters", "workload")
    op.create_unique_constraint(
        "uq_budget_counter_scope", "model_budget_counters", ["project_id", "scope", "scope_id"]
    )
    for name in (
        "ck_budget_probe_project_limit_positive", "ck_budget_probe_task_limit_positive",
        "ck_budget_probe_run_limit_positive", "ck_budget_probe_project_tokens_positive",
        "ck_budget_probe_task_tokens_positive", "ck_budget_probe_run_tokens_positive",
    ):
        op.drop_constraint(name, "model_budget_policies", type_="check")
    for name in (
        "probe_project_limit_usd", "probe_task_limit_usd", "probe_run_limit_usd",
        "probe_project_token_limit", "probe_task_token_limit", "probe_run_token_limit",
    ):
        op.drop_column("model_budget_policies", name)
