"""decision service: decisions, labelled outcomes, calibration and downgrades (P4-T08, P4-T09)

Revision ID: 0013
Revises: 0011
Create Date: 2026-09-25 22:00:00.000000

Hand-written (parallel increment-4 streams; the coordinator re-chains down_revision on merge).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013"
down_revision = "0011"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "decision",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=True),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("task_id", sa.String(length=40), nullable=True),
        sa.Column("agent_id", sa.String(length=80), nullable=True),
        sa.Column("purpose", sa.String(length=60), nullable=False),
        sa.Column("authority", sa.String(length=30), nullable=False),
        sa.Column("backend", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("inputs_hash", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=240), nullable=True),
        sa.Column("options", JSONB, nullable=False),
        sa.Column("answer", JSONB, nullable=True),
        sa.Column("proposal", JSONB, nullable=True),
        sa.Column("probabilities", JSONB, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column("attempts", JSONB, nullable=False),
        sa.Column("enforced", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_decision_workspace_id"), "decision", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_decision_run_id"), "decision", ["run_id"], unique=False)
    op.create_index(op.f("ix_decision_purpose"), "decision", ["purpose"], unique=False)
    op.create_index(op.f("ix_decision_subject"), "decision", ["subject"], unique=False)

    op.create_table(
        "decision_outcome",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("decision_id", sa.String(length=40), nullable=False),
        sa.Column("purpose", sa.String(length=60), nullable=False),
        sa.Column("backend", sa.String(length=30), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=True),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], ["decision.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_decision_outcome_decision_id"), "decision_outcome", ["decision_id"], unique=False)
    op.create_index(op.f("ix_decision_outcome_purpose"), "decision_outcome", ["purpose"], unique=False)
    op.create_index(op.f("ix_decision_outcome_workspace_id"), "decision_outcome", ["workspace_id"], unique=False)

    op.create_table(
        "decision_calibration",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("purpose", sa.String(length=60), nullable=False),
        sa.Column("backend", sa.String(length=30), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("brier", sa.Float(), nullable=True),
        sa.Column("ece", sa.Float(), nullable=True),
        sa.Column("max_brier", sa.Float(), nullable=True),
        sa.Column("max_ece", sa.Float(), nullable=True),
        sa.Column("downgraded", sa.Boolean(), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_decision_calibration_purpose"), "decision_calibration", ["purpose"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_decision_calibration_purpose"), table_name="decision_calibration")
    op.drop_table("decision_calibration")
    op.drop_index(op.f("ix_decision_outcome_workspace_id"), table_name="decision_outcome")
    op.drop_index(op.f("ix_decision_outcome_purpose"), table_name="decision_outcome")
    op.drop_index(op.f("ix_decision_outcome_decision_id"), table_name="decision_outcome")
    op.drop_table("decision_outcome")
    op.drop_index(op.f("ix_decision_subject"), table_name="decision")
    op.drop_index(op.f("ix_decision_purpose"), table_name="decision")
    op.drop_index(op.f("ix_decision_run_id"), table_name="decision")
    op.drop_index(op.f("ix_decision_workspace_id"), table_name="decision")
    op.drop_table("decision")
