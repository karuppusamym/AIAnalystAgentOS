"""isolated compute pools (ADR-0022, P7-06): worker_task and worker_task_event, the control plane's record
of tasks dispatched to compute-py / compute-ml and the events their workers reported

Revision ID: 0038
Revises: 0035
Create Date: 2026-09-26 20:00:00

Hand-written; parallel P7 streams: the integrator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_task",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("work_order_id", sa.String(64), nullable=True),
        sa.Column("pool", sa.String(40), nullable=False),
        sa.Column("capability_id", sa.String(120), nullable=False),
        sa.Column("capability_version", sa.String(40), nullable=False),
        sa.Column("capability_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("envelope_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("inputs", postgresql.JSONB(), nullable=False),
        sa.Column("outputs", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("usage", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_worker_task_workspace_id", "worker_task", ["workspace_id"])
    op.create_index("ix_worker_task_run_id", "worker_task", ["run_id"])
    op.create_index("ix_worker_task_idempotency_key", "worker_task", ["idempotency_key"])
    op.create_table(
        "worker_task_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("worker_task.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("worker_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_worker_task_event_task_id", "worker_task_event", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_worker_task_event_task_id", table_name="worker_task_event")
    op.drop_table("worker_task_event")
    op.drop_index("ix_worker_task_idempotency_key", table_name="worker_task")
    op.drop_index("ix_worker_task_run_id", table_name="worker_task")
    op.drop_index("ix_worker_task_workspace_id", table_name="worker_task")
    op.drop_table("worker_task")
