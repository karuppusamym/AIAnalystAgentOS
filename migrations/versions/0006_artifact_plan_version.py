"""artifact plan_version (P4-C07: replan supersedes run artifacts)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-25 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('artifact', sa.Column('plan_version', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('artifact', 'plan_version')
