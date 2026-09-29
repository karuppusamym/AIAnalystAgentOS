"""general entity matching (INT-004, N-7): match runs and their proposed pairs

`entity_match_run` records one deterministic record-linkage run between two tables (spec, digests of the
compared features, stats, the approval and, once promoted, the crosswalk table and its join keys);
`entity_match_pair` holds each proposed link with its score, band and a person's decision. No PII value is
stored in either table.

Revision ID: 0054_n7
Revises: 0045
Create Date: 2026-09-28 12:00:00

Hand-written; parallel streams: the integrator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054_n7"
down_revision = "0045"
branch_labels = None
depends_on = None

JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "entity_match_run",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("spec", JSON, nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("left_asset", sa.String(300), nullable=False),
        sa.Column("right_asset", sa.String(300), nullable=False),
        sa.Column("left_source_id", sa.String(40), nullable=True),
        sa.Column("right_source_id", sa.String(40), nullable=True),
        sa.Column("left_version", sa.String(64), nullable=True),
        sa.Column("right_version", sa.String(64), nullable=True),
        sa.Column("pii_fields", JSON, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stats", JSON, nullable=False),
        sa.Column("query_ids", JSON, nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("crosswalk_hash", sa.String(64), nullable=True),
        sa.Column("crosswalk_source_id", sa.String(40), nullable=True),
        sa.Column("crosswalk_table", sa.String(200), nullable=True),
        sa.Column("join_keys", JSON, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_entity_match_run_workspace_id", "entity_match_run", ["workspace_id"])
    op.create_table(
        "entity_match_pair",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("run_id", sa.String(40), sa.ForeignKey("entity_match_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("left_key", sa.String(300), nullable=False),
        sa.Column("right_key", sa.String(300), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("band", sa.String(10), nullable=False),
        sa.Column("fields", JSON, nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("decided_by", sa.String(80), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.UniqueConstraint("run_id", "left_key", "right_key", name="uq_entity_match_pair"),
    )
    op.create_index("ix_entity_match_pair_run_id", "entity_match_pair", ["run_id"])
    op.create_index("ix_entity_match_pair_workspace_id", "entity_match_pair", ["workspace_id"])


def downgrade() -> None:
    op.drop_table("entity_match_pair")
    op.drop_table("entity_match_run")
