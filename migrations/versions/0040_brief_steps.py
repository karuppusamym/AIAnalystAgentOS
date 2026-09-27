"""workspace brief, readiness assessments (P4-04); steps, branches, pins (P7-04/05) and notebooks (P7-12)

`workspace_brief` holds every version of a workspace's brief (assertions with origin, evidence, review
state and their own version); `readiness_assessment` one verdict per job kind and inputs. `step_branch`,
`analysis_step` and `analysis_step_version` are the step model of spec v4 §7 over runs, Ask threads and
notebooks; `step_pin` freezes one step version onto a tile or a schedule; `notebook` is a cell container.

Revision ID: 0040
Revises: 0035
Create Date: 2026-09-26 20:00:00

Hand-written; parallel P4/P7 streams: the integrator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0035"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=None if nullable else sa.func.now(), nullable=nullable)


def _ws() -> sa.Column:
    return sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "workspace_brief",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("assertions", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("workspace_id", "version", name="uq_workspace_brief_version"),
    )
    op.create_index("ix_workspace_brief_workspace_id", "workspace_brief", ["workspace_id"])

    op.create_table(
        "readiness_assessment",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("job_kind", sa.String(30), nullable=False),
        sa.Column("work_order_id", sa.String(40), nullable=True),
        sa.Column("work_order_revision", sa.Integer(), nullable=True),
        sa.Column("brief_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("checks", JSONB, nullable=False),
        sa.Column("inputs", JSONB, nullable=False),
        sa.Column("inputs_hash", sa.String(64), nullable=False),
        sa.Column("alternatives", JSONB, nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_readiness_assessment_workspace_id", "readiness_assessment", ["workspace_id"])
    op.create_index("ix_readiness_assessment_work_order_id", "readiness_assessment", ["work_order_id"])

    op.create_table(
        "step_branch",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("container_type", sa.String(20), nullable=False),
        sa.Column("container_id", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("parent_branch_id", sa.String(40), nullable=True),
        sa.Column("forked_from_step_id", sa.String(40), nullable=True),
        sa.Column("forked_from_version", sa.Integer(), nullable=True),
        sa.Column("base", JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("merged_into", JSONB, nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_step_branch_workspace_id", "step_branch", ["workspace_id"])
    op.create_index("ix_step_branch_container", "step_branch", ["container_type", "container_id"])

    op.create_table(
        "analysis_step",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("branch_id", sa.String(40), sa.ForeignKey("step_branch.id", ondelete="CASCADE"), nullable=False),
        sa.Column("container_type", sa.String(20), nullable=False),
        sa.Column("container_id", sa.String(40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("depends_on", JSONB, nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("origin", JSONB, nullable=False),
        sa.Column("forked_from", JSONB, nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_analysis_step_workspace_id", "analysis_step", ["workspace_id"])
    op.create_index("ix_analysis_step_branch_id", "analysis_step", ["branch_id"])
    op.create_index("ix_analysis_step_container", "analysis_step", ["container_type", "container_id"])

    op.create_table(
        "analysis_step_version",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("step_id", sa.String(40), sa.ForeignKey("analysis_step.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("inputs", JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("receipts", JSONB, nullable=False),
        sa.Column("result_snapshot", JSONB, nullable=True),
        sa.Column("chart_spec", JSONB, nullable=True),
        sa.Column("checks", JSONB, nullable=False),
        sa.Column("corrections", JSONB, nullable=False),
        sa.Column("verification_record_id", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
        _ts("finished_at", nullable=True),
        sa.UniqueConstraint("step_id", "version", name="uq_analysis_step_version"),
    )
    op.create_index("ix_analysis_step_version_step_id", "analysis_step_version", ["step_id"])
    op.create_index("ix_analysis_step_version_workspace_id", "analysis_step_version", ["workspace_id"])

    op.create_table(
        "step_pin",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("step_id", sa.String(40), nullable=False),
        sa.Column("step_version", sa.Integer(), nullable=False),
        sa.Column("target", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(40), nullable=True),
        sa.Column("frozen", JSONB, nullable=False),
        sa.Column("frozen_hash", sa.String(64), nullable=False),
        sa.Column("definition", JSONB, nullable=True),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("last_result", JSONB, nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_step_pin_workspace_id", "step_pin", ["workspace_id"])
    op.create_index("ix_step_pin_step_id", "step_pin", ["step_id"])

    op.create_table(
        "notebook",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_notebook_workspace_id", "notebook", ["workspace_id"])


def downgrade() -> None:
    for table in ("notebook", "step_pin", "analysis_step_version", "analysis_step", "step_branch", "readiness_assessment",
                  "workspace_brief"):
        op.drop_table(table)
