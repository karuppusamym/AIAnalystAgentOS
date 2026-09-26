"""transformation recipes and their runs (P6-04..P6-07, ADR-0023)

`recipe` holds each version of a recipe IR (draft | published | superseded); `recipe_run` records one
execution: plan (pushdown or snapshot fallback and why), join pre-flight, input snapshots, DQ gates,
schema-policy changes, materialized and quarantined outputs, column lineage and OpenLineage events.

Revision ID: 0036
Revises: 0030
Create Date: 2026-09-26 18:00:00

Hand-written; parallel increment streams: the coordinator re-chains `down_revision` on merge.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0033"
branch_labels = None
depends_on = None

JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "recipe",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("spec", JSON, nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(length=80), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", "version"),
    )
    op.create_index(op.f("ix_recipe_workspace_id"), "recipe", ["workspace_id"], unique=False)
    op.create_table(
        "recipe_run",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("recipe_id", sa.String(length=40), nullable=False),
        sa.Column("recipe_name", sa.String(length=60), nullable=False),
        sa.Column("recipe_version", sa.Integer(), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("engine", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("plan", JSON, nullable=False),
        sa.Column("preflight", JSON, nullable=False),
        sa.Column("snapshots", JSON, nullable=False),
        sa.Column("gates", JSON, nullable=False),
        sa.Column("schema_changes", JSON, nullable=False),
        sa.Column("outputs", JSON, nullable=False),
        sa.Column("lineage", JSON, nullable=False),
        sa.Column("openlineage", JSON, nullable=False),
        sa.Column("query_ids", JSON, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipe.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_recipe_run_workspace_id"), "recipe_run", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_recipe_run_recipe_id"), "recipe_run", ["recipe_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_recipe_run_recipe_id"), table_name="recipe_run")
    op.drop_index(op.f("ix_recipe_run_workspace_id"), table_name="recipe_run")
    op.drop_table("recipe_run")
    op.drop_index(op.f("ix_recipe_workspace_id"), table_name="recipe")
    op.drop_table("recipe")
