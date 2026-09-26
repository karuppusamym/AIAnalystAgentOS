"""knowledge review queue: AI-suggested and learned knowledge drafts with per-field provenance
and confidence (P4-K07, P4-K08)

Revision ID: 0024
Revises: 0018
Create Date: 2026-09-25 23:59:00

Hand-written; parallel increment-4 streams: the coordinator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_suggestion",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("origin", sa.String(length=60), nullable=False),
        sa.Column("proposed_by", sa.String(length=80), nullable=False),
        sa.Column("batch", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("decided_by", sa.String(length=40), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "subject", "content_hash", name="uq_knowledge_suggestion_content"),
    )
    op.create_index(op.f("ix_knowledge_suggestion_workspace_id"), "knowledge_suggestion", ["workspace_id"])
    op.create_index("ix_knowledge_suggestion_queue", "knowledge_suggestion", ["workspace_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_suggestion_queue", table_name="knowledge_suggestion")
    op.drop_index(op.f("ix_knowledge_suggestion_workspace_id"), table_name="knowledge_suggestion")
    op.drop_table("knowledge_suggestion")
