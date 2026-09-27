"""named owners: the business and technical owner of a workspace and of a source (P4-09)

* `workspace.owners`, `source.owners` (JSON, default `{}`): {"business": {name, email, user_id?},
  "technical": {...}}. Existing rows get `{}`: pilot readiness then lists both owners as missing.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-27 19:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace", sa.Column("owners", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("source", sa.Column("owners", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("source", "owners")
    op.drop_column("workspace", "owners")
