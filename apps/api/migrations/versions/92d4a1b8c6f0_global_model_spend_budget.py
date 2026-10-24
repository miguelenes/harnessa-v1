"""Add a lifetime model spend budget shared by projects and workloads."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "92d4a1b8c6f0"
down_revision = "71a5c2f80d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_global_spend_budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("max_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(18, 8), server_default="0", nullable=False),
        sa.Column("billed_usd", sa.Numeric(18, 8), server_default="0.0000228", nullable=False),
        sa.Column("estimated_usd", sa.Numeric(18, 8), server_default="0", nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_model_global_spend_budget_singleton"),
        sa.CheckConstraint("max_usd > 0", name="ck_model_global_spend_budget_max_positive"),
        sa.CheckConstraint("reserved_usd >= 0", name="ck_model_global_spend_budget_reserved_nonnegative"),
        sa.CheckConstraint("billed_usd >= 0", name="ck_model_global_spend_budget_billed_nonnegative"),
        sa.CheckConstraint("estimated_usd >= 0", name="ck_model_global_spend_budget_estimated_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Count the two already-recorded $0.0000114 M13 route checks in the user's
    # remaining-milestone ceiling, including databases created after those calls.
    op.execute(
        sa.text(
            "INSERT INTO model_global_spend_budgets (id, max_usd, reserved_usd, billed_usd, estimated_usd) "
            "VALUES (1, 20.00, 0, 0.0000228, 0)"
        )
    )


def downgrade() -> None:
    op.drop_table("model_global_spend_budgets")
