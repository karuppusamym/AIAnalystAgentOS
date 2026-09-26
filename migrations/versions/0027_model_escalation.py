"""model_call escalation record: cheap first, escalate (the large tier answered because a small-tier
answer failed deterministic validation)

Revision ID: 0027
Revises: 0024
Create Date: 2026-09-26 12:00:00

Hand-written; parallel increment-4 streams: the coordinator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_call", sa.Column("escalated_from", sa.String(length=120), nullable=True))
    op.add_column("model_call", sa.Column("escalation_reason", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("model_call", "escalation_reason")
    op.drop_column("model_call", "escalated_from")
