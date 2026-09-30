"""what-if scenarios: a governed SemanticQuery with declared changes, labelled observed vs simulated (N-9)

* `what_if_scenario`: the spec (query, adjustments, filter overrides) and its hash, the assumptions and
  their hash, the observed baseline's receipt, the labelled result and its hash, the semantic model and
  compiler versions it was computed against. Private to `created_by`; never a publishable artifact.

Revision ID: 0051_n9
Revises: 0050_n8
Create Date: 2026-09-28 16:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "0051_n9"
down_revision = "0050_n8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "what_if_scenario",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("ask_turn_id", sa.String(40), nullable=True),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("assumptions", sa.JSON(), nullable=False),
        sa.Column("assumptions_hash", sa.String(64), nullable=False),
        sa.Column("baseline", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("semantic_model_version", sa.Integer(), nullable=True),
        sa.Column("compiler_version", sa.String(40), nullable=True),
        sa.Column("scenario_version", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_what_if_scenario_workspace_id", "what_if_scenario", ["workspace_id"])
    op.create_index("ix_what_if_scenario_ask_turn_id", "what_if_scenario", ["ask_turn_id"])
    op.create_index("ix_what_if_scenario_created_by", "what_if_scenario", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_what_if_scenario_created_by", table_name="what_if_scenario")
    op.drop_index("ix_what_if_scenario_ask_turn_id", table_name="what_if_scenario")
    op.drop_index("ix_what_if_scenario_workspace_id", table_name="what_if_scenario")
    op.drop_table("what_if_scenario")
