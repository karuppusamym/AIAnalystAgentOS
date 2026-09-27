"""wave 3 backend: tested definitions and cross-environment promotion; revisions on monitors and Ask threads

* `definition.test_evidence`: the test run that moved a draft to `tested` (P7-11 query tools), bound to the
  content hash it tested; `definition.bindings`: per-environment connection bindings (ADR-0021 §5), outside
  the content hash; `definition.promoted_from`: the version a promotion copied, by content hash.
* `uq_definition_one_draft` now covers `tested` too: a tested draft is still the one editable draft.
* `monitor.revision`, `ask_thread.revision`: optional If-Match on PATCH (P4-06), existing rows start at 1.

Revision ID: 0041
Revises: 0037
Create Date: 2026-09-27 12:00:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0041"
down_revision = "0037"
branch_labels = None
depends_on = None

JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("definition", sa.Column("test_evidence", JSON, nullable=True))
    op.add_column("definition", sa.Column("bindings", JSON, nullable=True))
    op.add_column("definition", sa.Column("promoted_from", JSON, nullable=True))
    op.drop_index("uq_definition_one_draft", table_name="definition")
    op.create_index("uq_definition_one_draft", "definition", ["workspace_id", "kind", "key"], unique=True,
                    postgresql_where=sa.text("status IN ('draft', 'tested')"))
    op.add_column("monitor", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("ask_thread", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    op.drop_column("ask_thread", "revision")
    op.drop_column("monitor", "revision")
    op.execute("UPDATE definition SET status = 'draft' WHERE status = 'tested'")
    op.drop_index("uq_definition_one_draft", table_name="definition")
    op.create_index("uq_definition_one_draft", "definition", ["workspace_id", "kind", "key"], unique=True,
                    postgresql_where=sa.text("status = 'draft'"))
    op.drop_column("definition", "promoted_from")
    op.drop_column("definition", "bindings")
    op.drop_column("definition", "test_evidence")
