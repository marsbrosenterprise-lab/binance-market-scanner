"""Add non-executable MCP draft proposals."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_mcp_drafts"
down_revision: str | None = "0008_safety_candle_env"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "draft_proposals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("amount_asset", sa.String(length=16), nullable=False),
        sa.Column("entry_condition", sa.String(length=500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_conditions_json", sa.Text(), nullable=False),
        sa.Column("reasoning", sa.String(length=2000), nullable=False),
        sa.Column("uncertainty", sa.String(length=1000), nullable=False),
        sa.Column("snapshot_refs_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_draft_proposals_symbol", "draft_proposals", ["symbol"])
    op.create_index("ix_draft_proposals_state", "draft_proposals", ["state"])
    op.create_index("ix_draft_proposals_created_at", "draft_proposals", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_draft_proposals_created_at", table_name="draft_proposals")
    op.drop_index("ix_draft_proposals_state", table_name="draft_proposals")
    op.drop_index("ix_draft_proposals_symbol", table_name="draft_proposals")
    op.drop_table("draft_proposals")
