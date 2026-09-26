"""lite profile (ADR-0025, P7-16): spend_counter, the Postgres store of hard spend cap reservations
when Redis is absent (row-locked, atomic, fail closed)

Revision ID: 0035
Revises: 0030
Create Date: 2026-09-26 18:00:00

Hand-written; parallel P7 streams: the integrator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spend_counter",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_spend_counter_expires_at", "spend_counter", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_spend_counter_expires_at", table_name="spend_counter")
    op.drop_table("spend_counter")
