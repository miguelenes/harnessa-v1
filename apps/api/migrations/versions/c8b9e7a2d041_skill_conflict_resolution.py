"""Audit explicit rejection of overlapping skill candidates."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c8b9e7a2d041"
down_revision = "4a2f7c9d1e60"
branch_labels = None
depends_on = None

json_doc = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "skill_conflict_resolutions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("target_experience_id", sa.Uuid(), sa.ForeignKey("harness_experiences.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("conflict_key", sa.String(120), nullable=False),
        sa.Column("stable_key", sa.String(200), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("candidate_ids", json_doc, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "stable_key", name="uq_skill_conflict_resolution_key"),
        sa.CheckConstraint("action = 'reject_group'", name="ck_skill_conflict_resolution_action"),
    )
    op.create_index("ix_skill_conflict_resolutions_organization_id", "skill_conflict_resolutions", ["organization_id"])
    op.create_index("ix_skill_conflict_resolutions_project_id", "skill_conflict_resolutions", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_conflict_resolutions_project_id", table_name="skill_conflict_resolutions")
    op.drop_index("ix_skill_conflict_resolutions_organization_id", table_name="skill_conflict_resolutions")
    op.drop_table("skill_conflict_resolutions")
