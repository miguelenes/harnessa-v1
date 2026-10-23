"""Support repository-specific post-merge archive pull requests."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e1a640a2d901"
down_revision = "c17f49e6a302"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "github_repository_links",
        sa.Column(
            "archive_policy", sa.String(length=20), server_default="pre_merge", nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_github_link_archive_policy",
        "github_repository_links",
        "archive_policy IN ('pre_merge','post_merge')",
    )
    op.add_column(
        "github_sync_objects",
        sa.Column("object_purpose", sa.String(length=32), server_default="review", nullable=False),
    )
    op.create_check_constraint(
        "ck_github_object_purpose",
        "github_sync_objects",
        "object_purpose IN ('review','maintenance_archive')",
    )
    op.drop_constraint("ck_post_integration_step_status", "post_integration_steps", type_="check")
    op.create_check_constraint(
        "ck_post_integration_step_status",
        "post_integration_steps",
        "status IN ('pending','running','succeeded','failed','skipped','awaiting_merge')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE post_integration_steps SET status = 'pending' WHERE status = 'awaiting_merge'"
    )
    op.drop_constraint("ck_post_integration_step_status", "post_integration_steps", type_="check")
    op.create_check_constraint(
        "ck_post_integration_step_status",
        "post_integration_steps",
        "status IN ('pending','running','succeeded','failed','skipped')",
    )
    op.drop_constraint("ck_github_object_purpose", "github_sync_objects", type_="check")
    op.drop_column("github_sync_objects", "object_purpose")
    op.drop_constraint("ck_github_link_archive_policy", "github_repository_links", type_="check")
    op.drop_column("github_repository_links", "archive_policy")
