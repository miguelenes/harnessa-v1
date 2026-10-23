"""Persist per-resource GitHub rate-limit retry times."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3b8c9d1a012"
down_revision = "e1a640a2d901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "github_installations",
        sa.Column("retry_after_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "github_repository_links",
        sa.Column("retry_after_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "github_sync_objects",
        sa.Column("retry_after_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("github_sync_objects", "retry_after_at")
    op.drop_column("github_repository_links", "retry_after_at")
    op.drop_column("github_installations", "retry_after_at")
