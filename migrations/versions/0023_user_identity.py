"""OIDC identities linked to local users (P4-S04, SEC-001..003)

Revision ID: 0023
Revises: 0014
Create Date: 2026-09-25 23:30:00

Hand-written; parallel increment-4 streams: the coordinator re-chains `down_revision` on merge.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_identity",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("groups", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("managed_memberships", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject"),
    )
    op.create_index(op.f("ix_user_identity_user_id"), "user_identity", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_user_identity_user_id"), table_name="user_identity")
    op.drop_table("user_identity")
