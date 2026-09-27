"""Create the live Convert request ledger.

Revision ID: 0004_convert_requests
Revises: 0003_trade_executions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_convert_requests"
down_revision: str | None = "0003_trade_executions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "convert_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("from_asset", sa.String(length=16), nullable=False),
        sa.Column("to_asset", sa.String(length=16), nullable=False),
        sa.Column("from_amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("quote_id", sa.String(length=128), nullable=True),
        sa.Column("quote_json", sa.Text(), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column("order_status", sa.String(length=32), nullable=True),
        sa.Column("approval_actor", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quote_id"),
    )
    op.create_index("ix_convert_requests_state", "convert_requests", ["state"])
    op.create_index("ix_convert_requests_quote_id", "convert_requests", ["quote_id"])
    op.create_index("ix_convert_requests_order_id", "convert_requests", ["order_id"])
    op.create_index("ix_convert_requests_created_at", "convert_requests", ["created_at"])


def downgrade() -> None:
    op.drop_table("convert_requests")
