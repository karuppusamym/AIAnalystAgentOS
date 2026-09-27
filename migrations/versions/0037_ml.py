"""governed classical ML (P5-01..P5-06, ADR-0024): split manifests, experiments, model versions, scoring runs

`ml_split` is an immutable split manifest whose holdout is consumed once; `ml_experiment` indexes one
experiment's artifacts and is the platform record a package hash must match to load; `ml_model_version` is
the registry (one champion per model, the replaced champion kept for rollback); `ml_scoring_run` records one
approved batch scoring (pinned model and input versions, rows scored and rejected, the managed outputs).

Revision ID: 0037
Revises: 0035
Create Date: 2026-09-26 20:00:00

Hand-written; parallel streams: the integrator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037"
down_revision = "0038"
branch_labels = None
depends_on = None

JSON = postgresql.JSONB(astext_type=sa.Text())


def _ts(name: str = "created_at") -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "ml_split",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("dataset_version", sa.String(64), nullable=False),
        sa.Column("strategy", sa.String(30), nullable=False),
        sa.Column("manifest", JSON, nullable=False),
        sa.Column("membership_hash", sa.String(64), nullable=False),
        sa.Column("holdout_consumed_by", sa.String(40), nullable=True),
        sa.Column("holdout_spec_hash", sa.String(64), nullable=True),
        sa.Column("holdout_consumed_at", sa.DateTime(timezone=True), nullable=True),
        _ts(),
        sa.UniqueConstraint("workspace_id", "manifest_hash", name="uq_ml_split_manifest"),
    )
    op.create_index("ix_ml_split_workspace_id", "ml_split", ["workspace_id"])
    op.create_table(
        "ml_experiment",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=True),
        sa.Column("definition_id", sa.String(40), nullable=True),
        sa.Column("definition_key", sa.String(120), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=True),
        sa.Column("task", sa.String(20), nullable=False),
        sa.Column("spec", JSON, nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("verdict", sa.String(30), nullable=True),
        sa.Column("dataset_asset", sa.String(300), nullable=False),
        sa.Column("dataset_source_id", sa.String(40), nullable=True),
        sa.Column("dataset_version", sa.String(64), nullable=True),
        sa.Column("split_id", sa.String(40), nullable=True),
        sa.Column("manifest_hash", sa.String(64), nullable=True),
        sa.Column("selection_hash", sa.String(64), nullable=True),
        sa.Column("evaluation_seal", sa.String(64), nullable=True),
        sa.Column("package_hash", sa.String(64), nullable=True),
        sa.Column("code_digest", sa.String(64), nullable=True),
        sa.Column("environment_digest", sa.String(64), nullable=True),
        sa.Column("readiness", JSON, nullable=False),
        sa.Column("summary", JSON, nullable=False),
        sa.Column("artifacts", JSON, nullable=False),
        sa.Column("verification_record_id", sa.String(40), nullable=True),
        sa.Column("query_ids", JSON, nullable=False),
        sa.Column("reproduction_of", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts(),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ml_experiment_workspace_id", "ml_experiment", ["workspace_id"])
    op.create_index("ix_ml_experiment_run_id", "ml_experiment", ["run_id"])
    op.create_index("ix_ml_experiment_package_hash", "ml_experiment", ["package_hash"])
    op.create_table(
        "ml_model_version",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("experiment_id", sa.String(40), sa.ForeignKey("ml_experiment.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task", sa.String(20), nullable=False),
        sa.Column("package_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("feature_schema", JSON, nullable=False),
        sa.Column("metrics", JSON, nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("previous_champion_id", sa.String(40), nullable=True),
        sa.Column("promoted_by", sa.String(80), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts(),
        sa.UniqueConstraint("workspace_id", "name", "version", name="uq_ml_model_version"),
    )
    op.create_index("ix_ml_model_version_workspace_id", "ml_model_version", ["workspace_id"])
    op.create_index("ix_ml_model_version_experiment_id", "ml_model_version", ["experiment_id"])
    op.create_index("uq_ml_model_one_champion", "ml_model_version", ["workspace_id", "name"], unique=True,
                    postgresql_where=sa.text("status = 'champion'"))
    op.create_table(
        "ml_scoring_run",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=True),
        sa.Column("definition_id", sa.String(40), nullable=False),
        sa.Column("definition_key", sa.String(120), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("model_version_id", sa.String(40), nullable=False),
        sa.Column("package_hash", sa.String(64), nullable=False),
        sa.Column("input_asset", sa.String(300), nullable=True),
        sa.Column("input_source_id", sa.String(40), nullable=True),
        sa.Column("input_version", sa.String(64), nullable=True),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("rows_input", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_scored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_source_id", sa.String(40), nullable=True),
        sa.Column("output_table", sa.String(200), nullable=True),
        sa.Column("rejected_table", sa.String(200), nullable=True),
        sa.Column("details", JSON, nullable=False),
        sa.Column("query_ids", JSON, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts(),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ml_scoring_run_workspace_id", "ml_scoring_run", ["workspace_id"])
    op.create_index("ix_ml_scoring_run_model_version_id", "ml_scoring_run", ["model_version_id"])
    op.create_index("ix_ml_scoring_run_dedupe_key", "ml_scoring_run", ["dedupe_key"])


def downgrade() -> None:
    op.drop_table("ml_scoring_run")
    op.drop_index("uq_ml_model_one_champion", table_name="ml_model_version")
    op.drop_table("ml_model_version")
    op.drop_table("ml_experiment")
    op.drop_table("ml_split")
