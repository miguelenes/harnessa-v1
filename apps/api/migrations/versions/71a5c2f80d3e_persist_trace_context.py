"""Persist W3C trace context across task dispatch and worker boundaries."""

import sqlalchemy as sa
from alembic import op

revision = "71a5c2f80d3e"
down_revision = "e907c29db3fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("traceparent", sa.String(length=55), nullable=True))
    op.add_column("runs", sa.Column("traceparent", sa.String(length=55), nullable=True))
    op.add_column(
        "github_webhook_deliveries", sa.Column("traceparent", sa.String(length=55), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("github_webhook_deliveries", "traceparent")
    op.drop_column("runs", "traceparent")
    op.drop_column("tasks", "traceparent")
