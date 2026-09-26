"""semantic compilation: relationship review queue, model-version approvals, governance label on findings
(P7-02, P7-09, P4-05)

Revision ID: 0034
Revises: 0030
Create Date: 2026-09-26 18:00:00

Hand-written; parallel increment-4 streams: the coordinator re-chains `down_revision` on merge.
Existing findings are `ad_hoc` (no finding was compiled from a SemanticQuery before this revision).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "semantic_relationship_candidate",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.String(length=40), nullable=True),
        sa.Column("from_asset", sa.String(length=400), nullable=False),
        sa.Column("from_columns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("to_asset", sa.String(length=400), nullable=False),
        sa.Column("to_columns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cardinality", sa.String(length=20), nullable=False),
        sa.Column("containment", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("assessment", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("origin", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("proposed_by", sa.String(length=40), nullable=False),
        sa.Column("approval_id", sa.String(length=40), nullable=True),
        sa.Column("decided_by", sa.String(length=40), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("relationship_name", sa.String(length=200), nullable=True),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_semantic_relationship_candidate_workspace_id"), "semantic_relationship_candidate",
                    ["workspace_id"], unique=False)
    op.create_index(op.f("ix_semantic_relationship_candidate_approval_id"), "semantic_relationship_candidate",
                    ["approval_id"], unique=False)
    op.add_column("semantic_model", sa.Column("approval_id", sa.String(length=40), nullable=True))
    op.add_column("semantic_model", sa.Column("decided_by", sa.String(length=40), nullable=True))
    op.add_column("semantic_model", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_semantic_model_approval_id"), "semantic_model", ["approval_id"], unique=False)
    op.add_column("insight", sa.Column("governance", sa.String(length=10), server_default="ad_hoc", nullable=False))
    op.add_column("insight", sa.Column("semantic", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("insight", "semantic")
    op.drop_column("insight", "governance")
    op.drop_index(op.f("ix_semantic_model_approval_id"), table_name="semantic_model")
    op.drop_column("semantic_model", "decided_at")
    op.drop_column("semantic_model", "decided_by")
    op.drop_column("semantic_model", "approval_id")
    op.drop_index(op.f("ix_semantic_relationship_candidate_approval_id"), table_name="semantic_relationship_candidate")
    op.drop_index(op.f("ix_semantic_relationship_candidate_workspace_id"), table_name="semantic_relationship_candidate")
    op.drop_table("semantic_relationship_candidate")
