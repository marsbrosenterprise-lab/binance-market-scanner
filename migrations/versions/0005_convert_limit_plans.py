"""Add application-managed Limit Convert plan fields.

Revision ID: 0005_convert_limit_plans
Revises: 0004_convert_requests
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_convert_limit_plans"
down_revision: str | None = "0004_convert_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("convert_requests", sa.Column("limit_price", sa.Numeric(38, 18)))
    op.add_column("convert_requests", sa.Column("trigger_direction", sa.String(length=16)))
    op.add_column("convert_requests", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("convert_requests", sa.Column("triggered_at", sa.DateTime(timezone=True)))
    op.create_index("ix_convert_requests_expires_at", "convert_requests", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_convert_requests_expires_at", table_name="convert_requests")
    op.drop_column("convert_requests", "triggered_at")
    op.drop_column("convert_requests", "expires_at")
    op.drop_column("convert_requests", "trigger_direction")
    op.drop_column("convert_requests", "limit_price")
