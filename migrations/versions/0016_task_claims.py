"""optimistic task claims and per-artifact lineage lookups (P4-S02)

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-25 23:00:00.000000

Hand-written.
"""
from alembic import op
import sqlalchemy as sa


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_task", sa.Column("claim_version", sa.Integer(), server_default="0", nullable=False))
    op.create_index("ix_lineage_edge_workspace_to", "lineage_edge", ["workspace_id", "to_type", "to_id"])


def downgrade() -> None:
    op.drop_index("ix_lineage_edge_workspace_to", table_name="lineage_edge")
    op.drop_column("run_task", "claim_version")
