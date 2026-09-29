"""external delivery: approved email/webhook destinations and their deliveries (N-3)

* `delivery_destination`: a workspace's email recipients or webhook URL, authorized by an approval bound to
  `destination_hash`; a changed target clears `authorized_hash` and needs a new approval.
* `delivery`: one send of a verified report snapshot or an alert, with a unique idempotency key, bounded
  retries (`attempts`, `next_attempt_at`, a `locked_until` lease) and terminal `delivered`/`dead_letter`/`refused`.

Revision ID: 0047_n3
Revises: 0045
Create Date: 2026-09-28 14:00:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047_n3"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_destination",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("content_kinds", postgresql.JSONB(), nullable=False),
        sa.Column("destination_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("authorized_hash", sa.String(64), nullable=True),
        sa.Column("authorized_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "name"),
    )
    op.create_index("ix_delivery_destination_workspace_id", "delivery_destination", ["workspace_id"])
    op.create_table(
        "delivery",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), nullable=False),
        sa.Column("destination_id", sa.String(40), nullable=False),
        sa.Column("destination_hash", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.String(40), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("response", postgresql.JSONB(), nullable=False),
        sa.Column("origin", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_delivery_workspace_id", "delivery", ["workspace_id"])
    op.create_index("ix_delivery_destination_id", "delivery", ["destination_id"])
    op.create_index("ix_delivery_status", "delivery", ["status"])
    op.create_index("ix_delivery_next_attempt_at", "delivery", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_table("delivery")
    op.drop_table("delivery_destination")
