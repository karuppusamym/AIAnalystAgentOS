"""build targets and build jobs: the write path (P4-E06 BuildGateway, P4-E04 dbt builder)

Revision ID: 0020
Revises: 0014
Create Date: 2026-09-25 23:30:00

Hand-written (parallel increment-4 streams; the coordinator re-chains `down_revision` on merge).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "build_target",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("engine", sa.String(80), nullable=False),
        sa.Column("schema_name", sa.String(63), nullable=False),
        sa.Column("build_role", sa.String(63), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("provisioning", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "engine", "schema_name"),
    )
    op.create_index("ix_build_target_workspace_id", "build_target", ["workspace_id"])
    op.create_table(
        "build_job",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("source_run_id", sa.String(40), nullable=False),
        sa.Column("artifact_id", sa.String(40), nullable=True),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("engine", sa.String(80), nullable=False),
        sa.Column("runner", sa.String(40), nullable=False, server_default="dbt-core"),
        sa.Column("target_schema", sa.String(63), nullable=False),
        sa.Column("project_name", sa.String(120), nullable=False),
        sa.Column("project_files", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("project_hash", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=True),
        sa.Column("relations", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("dry_run", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("estimate", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("rollback", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="planned"),
        sa.Column("manifest", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("run_results", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("openlineage", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("log_tail", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_build_job_workspace_id", "build_job", ["workspace_id"])
    op.create_index("ix_build_job_run_id", "build_job", ["run_id"])
    op.create_index("ix_build_job_source_run_id", "build_job", ["source_run_id"])


def downgrade() -> None:
    op.drop_index("ix_build_job_source_run_id", table_name="build_job")
    op.drop_index("ix_build_job_run_id", table_name="build_job")
    op.drop_index("ix_build_job_workspace_id", table_name="build_job")
    op.drop_table("build_job")
    op.drop_index("ix_build_target_workspace_id", table_name="build_target")
    op.drop_table("build_target")
