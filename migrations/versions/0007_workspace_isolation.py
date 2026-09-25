"""workspace isolation: workspace_id in the lineage_edge uniqueness key (P4-C03)

Hand-written. Written in parallel with 0006 (another increment-4 row): it revises 0005 here and the
coordinator re-chains ``down_revision`` to "0006" when both are merged.

Revision ID: 0007
Revises: 0005
Create Date: 2026-09-25
"""
from alembic import op

revision = "0007"
down_revision = "0009"
branch_labels = None
depends_on = None

OLD = "lineage_edge_from_type_from_id_relation_to_type_to_id_key"  # Postgres' default name from 0001
NEW = "uq_lineage_edge_workspace_edge"


def upgrade() -> None:
    op.execute(f"ALTER TABLE lineage_edge DROP CONSTRAINT IF EXISTS {OLD}")
    op.create_unique_constraint(NEW, "lineage_edge", ["workspace_id", "from_type", "from_id", "relation", "to_type", "to_id"])


def downgrade() -> None:
    # The narrower key cannot hold the same edge in two workspaces: keep the oldest copy of each.
    op.execute(
        "DELETE FROM lineage_edge a USING lineage_edge b WHERE a.id > b.id AND a.from_type = b.from_type "
        "AND a.from_id = b.from_id AND a.relation = b.relation AND a.to_type = b.to_type AND a.to_id = b.to_id"
    )
    op.drop_constraint(NEW, "lineage_edge", type_="unique")
    op.create_unique_constraint(OLD, "lineage_edge", ["from_type", "from_id", "relation", "to_type", "to_id"])
