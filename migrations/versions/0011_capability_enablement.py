"""capability enablement per workspace and run capability bindings (P4-X01)

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-25 20:00:00.000000

Hand-written.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_run", sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()),
                                            server_default="{}", nullable=False))
    op.create_table(
        "workspace_capability",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("capability_id", sa.String(length=160), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "capability_id"),
    )
    op.create_index(op.f("ix_workspace_capability_workspace_id"), "workspace_capability", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_workspace_capability_workspace_id"), table_name="workspace_capability")
    op.drop_table("workspace_capability")
    op.drop_column("analysis_run", "capabilities")
