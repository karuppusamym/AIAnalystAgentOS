"""ask turn analysis: the analyst-mode record of an Ask turn

* `ask_turn.analysis` (JSON, NULL for quick turns): {mode, plan {approach, origin, steps}, steps [per step: question,
  status, sql, result (<= 50 rows + true row_count), facts, series, comparison, drivers, checks, refusal],
  synthesis {text, citations, origin, rejected?, stale?}, follow_ups, headline_step}. The turn's sql/result/chart
  mirror the headline step, so every existing screen reads an analyst turn like a quick one.

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-28 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ask_turn", sa.Column("analysis", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("ask_turn", "analysis")
