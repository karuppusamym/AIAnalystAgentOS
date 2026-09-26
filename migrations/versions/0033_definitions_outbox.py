"""definition versions and schedule pins (P7-03, ADR-0021); idempotency records, dispatch outbox,
typed work orders and schedule revisions (P4-06)

Existing schedules start at revision 1 with no pins: a re-analysis schedule pins its baseline run
the next time one completes (or when an owner accepts an upgrade). Nothing is backfilled.

Revision ID: 0033
Revises: 0030
Create Date: 2026-09-26 18:00:00

Hand-written; parallel increment-4 streams: the coordinator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0030"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=None if nullable else sa.func.now(), nullable=nullable)


def upgrade() -> None:
    op.add_column("schedule", sa.Column("revision", sa.Integer(), server_default="1", nullable=False))
    op.add_column("schedule", sa.Column("pins", JSONB, server_default="{}", nullable=False))
    op.add_column("schedule", sa.Column("pin_status", JSONB, server_default="{}", nullable=False))

    op.create_table(
        "definition",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("published_by", sa.String(80), nullable=True),
        _ts("published_at", nullable=True),
        sa.Column("retired_by", sa.String(80), nullable=True),
        _ts("retired_at", nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("workspace_id", "kind", "key", "version", name="uq_definition_version"),
    )
    op.create_index("ix_definition_workspace_id", "definition", ["workspace_id"])
    op.create_index("uq_definition_one_draft", "definition", ["workspace_id", "kind", "key"], unique=True,
                    postgresql_where=sa.text("status = 'draft'"))

    op.create_table(
        "idempotency_record",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("principal", sa.String(80), nullable=False),
        sa.Column("workspace_id", sa.String(40), nullable=False),
        sa.Column("operation", sa.String(60), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response", JSONB, nullable=True),
        sa.Column("resource_type", sa.String(40), nullable=True),
        sa.Column("resource_id", sa.String(80), nullable=True),
        _ts("locked_until", nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _ts("created_at"),
        _ts("completed_at", nullable=True),
        sa.UniqueConstraint("principal", "workspace_id", "operation", "key", name="uq_idempotency_scope"),
    )
    op.create_index("ix_idempotency_record_expires_at", "idempotency_record", ["expires_at"])

    op.create_table(
        "dispatch_outbox",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("workflow_id", sa.String(120), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _ts("created_at"),
        _ts("dispatched_at", nullable=True),
        sa.UniqueConstraint("kind", "run_id", name="uq_dispatch_outbox_run"),
    )
    op.create_index("ix_dispatch_outbox_workspace_id", "dispatch_outbox", ["workspace_id"])
    op.create_index("ix_dispatch_outbox_pending", "dispatch_outbox", ["status", "available_at"])

    op.create_table(
        "work_order",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("spec_type", sa.String(30), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("run_ids", JSONB, nullable=False),
        sa.Column("created_by", sa.String(40), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_work_order_workspace_id", "work_order", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_work_order_workspace_id", table_name="work_order")
    op.drop_table("work_order")
    op.drop_index("ix_dispatch_outbox_pending", table_name="dispatch_outbox")
    op.drop_index("ix_dispatch_outbox_workspace_id", table_name="dispatch_outbox")
    op.drop_table("dispatch_outbox")
    op.drop_index("ix_idempotency_record_expires_at", table_name="idempotency_record")
    op.drop_table("idempotency_record")
    op.drop_index("uq_definition_one_draft", table_name="definition")
    op.drop_index("ix_definition_workspace_id", table_name="definition")
    op.drop_table("definition")
    op.drop_column("schedule", "pin_status")
    op.drop_column("schedule", "pins")
    op.drop_column("schedule", "revision")
