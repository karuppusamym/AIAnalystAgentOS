"""snapshot population on source assets (P4-C12)

Records what each staged snapshot is relative to its origin table: rows staged, the origin's row
count (and how it was measured), whether the row cap truncated it, and the declared sampling method.

Revision ID: 0008
Revises: 0005
Create Date: 2026-09-25 18:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0008'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('source_asset', sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), server_default='{}',
                                            nullable=False))


def downgrade() -> None:
    op.drop_column('source_asset', 'snapshot')
