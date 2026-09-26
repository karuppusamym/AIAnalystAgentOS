"""Ask threads and turns (P4-U02)

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-25 23:30:00.000000

Hand-written.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0021"
down_revision = "0020"  # re-chained after the build gateway (0020)
branch_labels = None
depends_on = None


def _json(name: str, default: str | None) -> sa.Column:
    if default is None:
        return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=default, nullable=False)


def upgrade() -> None:
    op.create_table(
        "ask_thread",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ask_thread_workspace_id"), "ask_thread", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_ask_thread_user_id"), "ask_thread", ["user_id"], unique=False)
    op.create_table(
        "ask_turn",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("thread_id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("seq", sa.Integer(), server_default="1", nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        _json("parameters", "{}"),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column("route", sa.String(length=30), nullable=True),
        sa.Column("answered_by", sa.String(length=20), nullable=True),
        _json("refusal", None),
        sa.Column("sql", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        _json("chart", None),
        _json("result", None),
        _json("verified_query", None),
        sa.Column("model", sa.String(length=120), nullable=True),
        _json("attempts", "[]"),
        _json("stages", "[]"),
        _json("decisions", "[]"),
        _json("provenance", "{}"),
        _json("promotions", "[]"),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["ask_thread.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ask_turn_thread_id"), "ask_turn", ["thread_id"], unique=False)
    op.create_index(op.f("ix_ask_turn_workspace_id"), "ask_turn", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ask_turn_workspace_id"), table_name="ask_turn")
    op.drop_index(op.f("ix_ask_turn_thread_id"), table_name="ask_turn")
    op.drop_table("ask_turn")
    op.drop_index(op.f("ix_ask_thread_user_id"), table_name="ask_thread")
    op.drop_index(op.f("ix_ask_thread_workspace_id"), table_name="ask_thread")
    op.drop_table("ask_thread")
