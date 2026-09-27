"""absent curated columns: a user-curated column missing from one crawl is kept and marked absent (P7-20)

* `source_column.absent_since` (timestamp, NULL = present). A column with a user business name, description
  or tags that a crawl or reload no longer sees keeps its curation and is hidden from queries, scopes and
  prompts; it is restored when it reappears. Uncurated columns are still deleted.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-27 20:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_column", sa.Column("absent_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("source_column", "absent_since")
