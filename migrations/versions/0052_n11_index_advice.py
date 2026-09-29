"""index advice: advisory index / partitioning / clustering recommendations (N-11)

* `index_advice`: one recommendation per (workspace, advice_key) with its DDL text, evidence (query
  fingerprints and ids, dry-plan scan nodes, crawler stats), estimated benefit and review status
  (open | acknowledged | dismissed | superseded). Nothing here is ever executed by the platform.

Revision ID: 0052_n11
Revises: 0045
Create Date: 2026-09-28 12:00:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052_n11"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "index_advice",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.String(40), nullable=False),
        sa.Column("asset", sa.String(330), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("columns", postgresql.JSONB(), nullable=False),
        sa.Column("dialect", sa.String(30), nullable=False),
        sa.Column("ddl", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(10), nullable=False),
        sa.Column("estimated_benefit", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("notes", postgresql.JSONB(), nullable=False),
        sa.Column("advice_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("status_note", sa.Text(), nullable=True),
        sa.Column("status_by", sa.String(80), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "advice_key", name="uq_index_advice_key"),
        sa.CheckConstraint("status IN ('open', 'acknowledged', 'dismissed', 'superseded')", name="ck_index_advice_status"),
    )
    op.create_index("ix_index_advice_workspace_id", "index_advice", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_index_advice_workspace_id", table_name="index_advice")
    op.drop_table("index_advice")
