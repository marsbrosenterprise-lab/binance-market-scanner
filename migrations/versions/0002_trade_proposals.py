"""Create versioned trade proposals and approval state.

Revision ID: 0002_trade_proposals
Revises: 0001_phase2_market_data
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_trade_proposals"
down_revision: str | None = "0001_phase2_market_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_proposals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("signal_json", sa.Text(), nullable=False),
        sa.Column("risk_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_actor", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trade_proposals_symbol", "trade_proposals", ["symbol"])
    op.create_index("ix_trade_proposals_state", "trade_proposals", ["state"])
    op.create_index("ix_trade_proposals_expires_at", "trade_proposals", ["expires_at"])
    op.create_index("ix_trade_proposals_created_at", "trade_proposals", ["created_at"])


def downgrade() -> None:
    op.drop_table("trade_proposals")
