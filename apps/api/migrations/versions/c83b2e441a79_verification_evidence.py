"""durable trusted verification evidence for governed retrieval"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c83b2e441a79"
down_revision = "af64c011b715"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("runs", sa.Column("source_revision", sa.String(64), nullable=True))
    op.create_table(
        "verification_evidence",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("source_record_id", sa.String(200), nullable=False),
        sa.Column("source_revision", sa.String(200), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("summary", sa.String(2000), nullable=False),
        sa.Column("criteria", JSON_DOCUMENT, nullable=False),
        sa.Column("artifact_digest", sa.String(64), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("verifier_id", sa.String(120), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("attempt > 0 AND fencing_token >= 0", name="ck_verification_attempt_fence"),
        sa.CheckConstraint("outcome IN ('passed','failed','manual_review_required','unverified')", name="ck_verification_outcome"),
        sa.CheckConstraint("length(source_record_id) > 0 AND length(source_revision) > 0", name="ck_verification_source_identity"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "source_record_id", name="uq_verification_source"),
    )
    op.create_index("ix_verification_evidence_scope", "verification_evidence", ["project_id", "outcome", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_verification_evidence_scope", table_name="verification_evidence")
    op.drop_table("verification_evidence")
    op.drop_column("runs", "source_revision")
