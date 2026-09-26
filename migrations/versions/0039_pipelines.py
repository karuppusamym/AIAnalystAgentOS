"""pipelines, pipeline runs, managed writer destinations and materializations (P6-01..P6-03)

`pipeline` holds each version of a PipelineSpec; `pipeline_run` one dry run or run (manifest, checks,
reconciliation, candidate); `writer_destination` the allowlisted schemas of the managed writer;
`materialization` each promoted/staged version of a destination table with its rollback pointer and
retry checkpoint. The writer login itself is provisioned by `analystos migrate` (like the builder).

Revision ID: 0039
Revises: 0035
Create Date: 2026-09-26 22:00:00

Hand-written; parallel increment streams: the integrator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039"
down_revision = "0035"
branch_labels = None
depends_on = None

JSON = postgresql.JSONB(astext_type=sa.Text())


def _ts(name: str, nullable: bool = False) -> sa.Column:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "pipeline",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("spec", JSON, nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        _ts("created_at"),
        _ts("published_at", nullable=True),
        sa.Column("published_by", sa.String(length=80), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", "version"),
    )
    op.create_index(op.f("ix_pipeline_workspace_id"), "pipeline", ["workspace_id"], unique=False)
    op.create_table(
        "pipeline_run",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("pipeline_id", sa.String(length=40), nullable=False),
        sa.Column("pipeline_name", sa.String(length=60), nullable=False),
        sa.Column("pipeline_version", sa.Integer(), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        *[sa.Column(c, JSON, nullable=False) for c in ("recipes", "plan", "manifest", "sql", "checks", "reconciliation",
                                                       "candidate", "recipe_run_ids", "query_ids")],
        sa.Column("plan_hash", sa.String(length=64), nullable=True),
        sa.Column("approval_id", sa.String(length=40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        _ts("created_at"),
        _ts("finished_at", nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipeline.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pipeline_run_workspace_id"), "pipeline_run", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_pipeline_run_pipeline_id"), "pipeline_run", ["pipeline_id"], unique=False)
    op.create_table(
        "writer_destination",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("engine", sa.String(length=80), nullable=False),
        sa.Column("schema_name", sa.String(length=63), nullable=False),
        sa.Column("tables", JSON, nullable=False),
        sa.Column("writer_role", sa.String(length=63), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provisioning", JSON, nullable=False),
        sa.Column("created_by", sa.String(length=40), nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engine", "schema_name"),
    )
    op.create_index(op.f("ix_writer_destination_workspace_id"), "writer_destination", ["workspace_id"], unique=False)
    op.create_table(
        "materialization",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("destination_id", sa.String(length=40), nullable=False),
        sa.Column("schema_name", sa.String(length=63), nullable=False),
        sa.Column("table_name", sa.String(length=63), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("version_table", sa.String(length=63), nullable=False),
        sa.Column("pipeline_id", sa.String(length=40), nullable=True),
        sa.Column("pipeline_run_id", sa.String(length=40), nullable=True),
        sa.Column("approval_id", sa.String(length=40), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("candidate", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("previous_id", sa.String(length=40), nullable=True),
        sa.Column("checkpoint", JSON, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        _ts("created_at"),
        _ts("promoted_at", nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["destination_id"], ["writer_destination.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("destination_id", "table_name", "version"),
    )
    for col in ("workspace_id", "destination_id", "pipeline_id", "pipeline_run_id"):
        op.create_index(op.f(f"ix_materialization_{col}"), "materialization", [col], unique=False)


def downgrade() -> None:
    for col in ("pipeline_run_id", "pipeline_id", "destination_id", "workspace_id"):
        op.drop_index(op.f(f"ix_materialization_{col}"), table_name="materialization")
    op.drop_table("materialization")
    op.drop_index(op.f("ix_writer_destination_workspace_id"), table_name="writer_destination")
    op.drop_table("writer_destination")
    op.drop_index(op.f("ix_pipeline_run_pipeline_id"), table_name="pipeline_run")
    op.drop_index(op.f("ix_pipeline_run_workspace_id"), table_name="pipeline_run")
    op.drop_table("pipeline_run")
    op.drop_index(op.f("ix_pipeline_workspace_id"), table_name="pipeline")
    op.drop_table("pipeline")
