"""verified-query and hypothesis registries (P4-T05)

Revision ID: 0015
Revises: 0011
Create Date: 2026-09-25 22:00:00.000000

Hand-written.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015"
down_revision = "0011"
branch_labels = None
depends_on = None


def _json(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=default, nullable=False)


def upgrade() -> None:
    op.create_table(
        "verified_query",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        _json("patterns", "[]"),
        sa.Column("sql_template", sa.Text(), nullable=False),
        _json("parameters", "[]"),
        sa.Column("source_id", sa.String(length=40), nullable=True),
        sa.Column("dialect", sa.String(length=40), server_default="postgres", nullable=False),
        _json("origin", "{}"),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("hits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name"),
    )
    op.create_index(op.f("ix_verified_query_workspace_id"), "verified_query", ["workspace_id"], unique=False)
    op.create_table(
        "registered_hypothesis",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        _json("spec", "{}"),
        sa.Column("question", sa.Text(), server_default="", nullable=False),
        sa.Column("statement", sa.Text(), server_default="", nullable=False),
        sa.Column("method", sa.String(length=60), nullable=False),
        sa.Column("asset", sa.String(length=300), nullable=False),
        sa.Column("question_key", sa.String(length=64), nullable=False),
        _json("claim", "[]"),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("last_outcome", sa.String(length=30), server_default="", nullable=False),
        _json("last_result", "{}"),
        sa.Column("origin", sa.String(length=20), server_default="agent", nullable=False),
        sa.Column("first_run_id", sa.String(length=40), nullable=False),
        sa.Column("last_run_id", sa.String(length=40), nullable=False),
        sa.Column("times_tested", sa.Integer(), server_default="0", nullable=False),
        sa.Column("times_verified", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "spec_hash"),
    )
    op.create_index(op.f("ix_registered_hypothesis_workspace_id"), "registered_hypothesis", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_registered_hypothesis_question_key"), "registered_hypothesis", ["question_key"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_registered_hypothesis_question_key"), table_name="registered_hypothesis")
    op.drop_index(op.f("ix_registered_hypothesis_workspace_id"), table_name="registered_hypothesis")
    op.drop_table("registered_hypothesis")
    op.drop_index(op.f("ix_verified_query_workspace_id"), table_name="verified_query")
    op.drop_table("verified_query")
