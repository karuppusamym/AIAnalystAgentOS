"""evidence citations: measured and document evidence cited as separate kinds (N-8)

* `evidence_citation_set`: one row per finding or Ask answer (unique subject_type + subject_id) with the
  `contracts/citations.py` document (quantitative citations with query/result hashes, document citations
  with document/section sha256, the source of each narrative number, conflicts between a document claim
  and measured data), counts for listing, and the cited document ids.

Revision ID: 0050_n8
Revises: 0048_n2
Create Date: 2026-09-28 12:00:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050_n8"
down_revision = "0048_n2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_citation_set",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=True),
        sa.Column("subject_type", sa.String(30), nullable=False),
        sa.Column("subject_id", sa.String(80), nullable=False),
        sa.Column("version", sa.String(30), nullable=False),
        sa.Column("quantitative_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("document_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conflict_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("document_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("citations", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("subject_type", "subject_id"),
    )
    op.create_index("ix_evidence_citation_set_workspace_id", "evidence_citation_set", ["workspace_id"])
    op.create_index("ix_evidence_citation_set_run_id", "evidence_citation_set", ["run_id"])
    op.create_index("ix_evidence_citation_set_conflict_count", "evidence_citation_set", ["conflict_count"])


def downgrade() -> None:
    op.drop_index("ix_evidence_citation_set_conflict_count", table_name="evidence_citation_set")
    op.drop_index("ix_evidence_citation_set_run_id", table_name="evidence_citation_set")
    op.drop_index("ix_evidence_citation_set_workspace_id", table_name="evidence_citation_set")
    op.drop_table("evidence_citation_set")
