"""existing-dashboard mode (N-2, BI-011/012): imported BI dashboards

* `bi_dashboard_import`: one version per import of an existing BI dashboard: inspection + fingerprint, mapping to
  sources and semantic metrics, gateway re-execution per chart, findings and proposals, and what an approved
  proposal changed in the BI tool (`applied`).

Revision ID: 0048_n2
Revises: 0045
Create Date: 2026-09-28 12:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "0048_n2"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bi_dashboard_import",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("destination", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("previous_fingerprint", sa.String(64), nullable=True),
        sa.Column("inspection", sa.JSON(), nullable=False),
        sa.Column("mapping", sa.JSON(), nullable=False),
        sa.Column("verification", sa.JSON(), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("proposals", sa.JSON(), nullable=False),
        sa.Column("applied", sa.JSON(), nullable=False),
        sa.Column("imported_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "destination", "external_id", "version", name="uq_bi_dashboard_import_version"),
    )
    op.create_index("ix_bi_dashboard_import_workspace_id", "bi_dashboard_import", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_bi_dashboard_import_workspace_id", table_name="bi_dashboard_import")
    op.drop_table("bi_dashboard_import")
