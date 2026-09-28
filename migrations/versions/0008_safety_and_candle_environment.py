"""Add persistent emergency stop and distinguish market-data environments."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_safety_and_candle_environment"
down_revision: str | None = "0007_convert_execution_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candles",
        sa.Column("environment", sa.String(length=16), nullable=False, server_default="sandbox"),
    )
    op.drop_constraint("candles_symbol_interval_open_time_key", "candles", type_="unique")
    op.create_unique_constraint(
        "uq_candles_symbol_interval_open_time_environment",
        "candles",
        ["symbol", "interval", "open_time", "environment"],
    )
    op.create_table(
        "safety_controls",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("emergency_stop", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(sa.text("INSERT INTO safety_controls (id, emergency_stop) VALUES (1, false)"))


def downgrade() -> None:
    op.drop_table("safety_controls")
    op.drop_constraint(
        "uq_candles_symbol_interval_open_time_environment", "candles", type_="unique"
    )
    op.create_unique_constraint(
        "candles_symbol_interval_open_time_key", "candles", ["symbol", "interval", "open_time"]
    )
    op.drop_column("candles", "environment")
