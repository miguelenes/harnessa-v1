"""Record optional post-integration hooks as skipped terminal outcomes."""

from __future__ import annotations

from alembic import op

revision = "c17f49e6a302"
down_revision = "a10b2f6d9c41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_post_integration_step_status", "post_integration_steps", type_="check"
    )
    op.create_check_constraint(
        "ck_post_integration_step_status",
        "post_integration_steps",
        "status IN ('pending','running','succeeded','failed','skipped')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE post_integration_steps SET status = 'failed' WHERE status = 'skipped'"
    )
    op.drop_constraint(
        "ck_post_integration_step_status", "post_integration_steps", type_="check"
    )
    op.create_check_constraint(
        "ck_post_integration_step_status",
        "post_integration_steps",
        "status IN ('pending','running','succeeded','failed')",
    )
