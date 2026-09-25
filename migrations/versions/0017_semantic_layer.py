"""workspace semantic layer: versioned semantic model and metric approval workflow (P4-K03)

Revision ID: 0017
Revises: 0014
Create Date: 2026-09-25 21:00:00.000000

Hand-written. down_revision is re-chained by the coordinator when the increment-4 rows merge.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0017"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "semantic_model",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("owner_id", sa.String(length=40), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("ai_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("datasets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("relationships", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("custom_extensions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("origin", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "version", name="uq_semantic_model_version"),
    )
    op.create_index(op.f("ix_semantic_model_workspace_id"), "semantic_model", ["workspace_id"], unique=False)
    op.create_table(
        "semantic_metric",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("normalized_expression", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=True),
        sa.Column("owner_id", sa.String(length=40), nullable=True),
        sa.Column("proposed_by", sa.String(length=40), nullable=False),
        sa.Column("proposed_via", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("approval_id", sa.String(length=40), nullable=True),
        sa.Column("decided_by", sa.String(length=40), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", "version", name="uq_semantic_metric_version"),
    )
    op.create_index(op.f("ix_semantic_metric_workspace_id"), "semantic_metric", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_semantic_metric_name"), "semantic_metric", ["name"], unique=False)
    op.create_index(op.f("ix_semantic_metric_approval_id"), "semantic_metric", ["approval_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_semantic_metric_approval_id"), table_name="semantic_metric")
    op.drop_index(op.f("ix_semantic_metric_name"), table_name="semantic_metric")
    op.drop_index(op.f("ix_semantic_metric_workspace_id"), table_name="semantic_metric")
    op.drop_table("semantic_metric")
    op.drop_index(op.f("ix_semantic_model_workspace_id"), table_name="semantic_model")
    op.drop_table("semantic_model")
