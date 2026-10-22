"""persist complete snapshot coverage baselines"""

import sqlalchemy as sa
from alembic import op

revision = "af64c011b715"
down_revision = "9f3c7a1d4e20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "retrieval_syncs",
        sa.Column("incremental", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "retrieval_syncs",
        sa.Column("snapshot_complete", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("retrieval_syncs", "snapshot_complete")
    op.drop_column("retrieval_syncs", "incremental")
