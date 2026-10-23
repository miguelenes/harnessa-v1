"""Persist late gateway usage observations and append reconciliation events."""

import sqlalchemy as sa
from alembic import op

revision = "8c7e2d1f4a90"
down_revision = "4a2d8c91e735"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_model_ledger_event", "model_request_ledger", type_="check")
    op.create_check_constraint(
        "ck_model_ledger_event",
        "model_request_ledger",
        "event_type IN ('reserved', 'attempt_started', 'attempt_failed', 'fallback_selected', 'attempt_succeeded', 'settled', 'stale_reconciled', 'usage_reconciled')",
    )
    op.add_column(
        "model_request_ledger",
        sa.Column("provider_reported_usd", sa.Numeric(18, 8), nullable=True),
    )
    op.create_table(
        "gateway_usage_imports",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_observation_id", sa.String(200), nullable=False),
        sa.Column("request_identifier", sa.String(200), nullable=False),
        sa.Column("gateway_billed_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("provider_reported_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("usage_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ledger_event_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('matched', 'retryable', 'ambiguous')", name="ck_gateway_usage_import_status"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "source_observation_id", name="uq_gateway_usage_source_observation"
        ),
    )
    op.create_index(
        "ix_gateway_usage_imports_organization_id", "gateway_usage_imports", ["organization_id"]
    )
    op.create_index("ix_gateway_usage_imports_project_id", "gateway_usage_imports", ["project_id"])
    op.create_index(
        "ix_gateway_usage_imports_request_identifier",
        "gateway_usage_imports",
        ["request_identifier"],
    )
    op.create_index("ix_gateway_usage_retry", "gateway_usage_imports", ["status", "next_retry_at"])


def downgrade() -> None:
    op.drop_table("gateway_usage_imports")
    op.drop_column("model_request_ledger", "provider_reported_usd")
    op.drop_constraint("ck_model_ledger_event", "model_request_ledger", type_="check")
    op.create_check_constraint(
        "ck_model_ledger_event",
        "model_request_ledger",
        "event_type IN ('reserved', 'attempt_started', 'attempt_failed', 'fallback_selected', 'attempt_succeeded', 'settled', 'stale_reconciled')",
    )
