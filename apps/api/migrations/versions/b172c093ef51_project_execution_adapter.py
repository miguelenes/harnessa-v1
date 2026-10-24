"""Persist project execution adapter selection."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b172c093ef51"
down_revision = "8a6f2c1e9d40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("execution_adapter", sa.String(32), server_default="deployment-default", nullable=False),
    )
    op.create_check_constraint(
        "ck_project_execution_adapter",
        "projects",
        "execution_adapter IN ('deployment-default', 'acp-opencode', 'disabled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_project_execution_adapter", "projects", type_="check")
    op.drop_column("projects", "execution_adapter")
