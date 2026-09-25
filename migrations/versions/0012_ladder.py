"""execution ladder rung and cost source on every model call (P4-T01, P4-T07)

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25 22:00:00

Hand-written. Existing rows are backfilled from what they already say: skips, refusals and
approval holds were answered by rules, cache hits by the cache, decision-provider calls by the
decision rung, low_cost calls by the small model and everything else by the strong model.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_call", sa.Column("answered_by", sa.String(length=20), nullable=True))
    op.add_column("model_call", sa.Column("cost_source", sa.String(length=60), nullable=True))
    op.execute("""
        UPDATE model_call SET answered_by = CASE
            WHEN status IN ('skipped', 'refused', 'approval_required') THEN 'rules'
            WHEN status = 'cache_hit' THEN 'cache'
            WHEN provider = 'typesafe' THEN 'decision'
            WHEN profile = 'low_cost' THEN 'llm_small'
            ELSE 'llm_large' END,
          cost_source = CASE WHEN status = 'ok' THEN 'provider' ELSE 'none' END
    """)
    op.alter_column("model_call", "answered_by", nullable=False, server_default="llm_large")
    op.create_index("ix_model_call_created_purpose_rung", "model_call", ["created_at", "purpose", "answered_by"])


def downgrade() -> None:
    op.drop_index("ix_model_call_created_purpose_rung", table_name="model_call")
    op.drop_column("model_call", "cost_source")
    op.drop_column("model_call", "answered_by")
