"""Add explicit armed automatic execution to Convert plans.

Revision ID: 0006_convert_auto_execution
Revises: 0005_convert_limit_plans
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_convert_auto_execution"
down_revision: str | None = "0005_convert_limit_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "convert_requests",
        sa.Column("auto_execute", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("convert_requests", sa.Column("armed_by", sa.String(length=128)))
    op.add_column("convert_requests", sa.Column("armed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("convert_requests", "armed_at")
    op.drop_column("convert_requests", "armed_by")
    op.drop_column("convert_requests", "auto_execute")
