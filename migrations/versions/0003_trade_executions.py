"""Create the execution ledger.

Revision ID: 0003_trade_executions
Revises: 0002_trade_proposals
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_trade_executions"
down_revision: str | None = "0002_trade_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_executions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("client_order_id", sa.String(length=36), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposal_id"),
        sa.UniqueConstraint("client_order_id"),
    )
    op.create_index("ix_trade_executions_proposal_id", "trade_executions", ["proposal_id"])
    op.create_index("ix_trade_executions_symbol", "trade_executions", ["symbol"])
    op.create_index("ix_trade_executions_status", "trade_executions", ["status"])
    op.create_index("ix_trade_executions_created_at", "trade_executions", ["created_at"])


def downgrade() -> None:
    op.drop_table("trade_executions")
