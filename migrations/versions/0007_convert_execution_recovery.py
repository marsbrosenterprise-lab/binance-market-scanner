"""Add durable Convert execution claims and reconciliation state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_convert_execution_recovery"
down_revision: str | None = "0006_convert_auto_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("convert_requests", sa.Column("execution_claim_id", sa.String(length=64)))
    op.add_column("convert_requests", sa.Column("execution_claimed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "convert_requests",
        sa.Column("execution_intent_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "convert_requests",
        sa.Column(
            "reconciliation_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_index(
        "ix_convert_requests_execution_claim_id",
        "convert_requests",
        ["execution_claim_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_convert_requests_execution_claim_id", table_name="convert_requests")
    op.drop_column("convert_requests", "reconciliation_required")
    op.drop_column("convert_requests", "execution_intent_json")
    op.drop_column("convert_requests", "execution_claimed_at")
    op.drop_column("convert_requests", "execution_claim_id")
