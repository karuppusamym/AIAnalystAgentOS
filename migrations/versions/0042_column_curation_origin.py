"""column curation origins: who wrote a column's business name and description

* `source_column.business_name_origin`, `source_column.description_origin` (source | rule | user), as on
  `source_asset`: a person's business name or description is `user` and no crawl or knowledge ingest
  overwrites it. Existing rows stay NULL (written by a crawl).

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-27 18:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_column", sa.Column("business_name_origin", sa.String(20), nullable=True))
    op.add_column("source_column", sa.Column("description_origin", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("source_column", "description_origin")
    op.drop_column("source_column", "business_name_origin")
