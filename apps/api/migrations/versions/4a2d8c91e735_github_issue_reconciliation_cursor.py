"""Persist bounded GitHub issue reconciliation progress."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "4a2d8c91e735"
down_revision = "f3b8c9d1a012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "github_repository_links",
        sa.Column("issue_reconciliation_page", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "github_repository_links",
        sa.Column("issue_reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("github_repository_links", "issue_reconciled_at")
    op.drop_column("github_repository_links", "issue_reconciliation_page")
