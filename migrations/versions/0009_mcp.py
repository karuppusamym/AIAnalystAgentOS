"""MCP client servers and MCP server clients, grants and usage (P4-X05, P4-X06)

Revision ID: 0009
Revises: 0005
Create Date: 2026-09-25

Hand-written. down_revision points at 0005 because increment-4 rows were built on parallel
branches; the coordinator re-chains 0006..0009 after merging them.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0005'  # re-chained by the coordinator after the parallel increment-4 merges
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table('mcp_server',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('workspace_id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('url', sa.String(length=500), nullable=False),
    sa.Column('transport', sa.String(length=30), nullable=False),
    sa.Column('secret_ref', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('allowed', sa.Boolean(), nullable=False),
    sa.Column('allowed_by', sa.String(length=40), nullable=True),
    sa.Column('config', JSONB, nullable=False),
    sa.Column('tools', JSONB, nullable=False),
    sa.Column('classifications', JSONB, nullable=False),
    sa.Column('last_refreshed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspace.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'name')
    )
    op.create_index(op.f('ix_mcp_server_workspace_id'), 'mcp_server', ['workspace_id'], unique=False)

    op.create_table('mcp_client',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('secret_hash', sa.String(length=64), nullable=False),
    sa.Column('service_user_id', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['service_user_id'], ['app_user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )

    op.create_table('mcp_grant',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('client_id', sa.String(length=40), nullable=False),
    sa.Column('workspace_id', sa.String(length=40), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('tools', JSONB, nullable=False),
    sa.Column('quotas', JSONB, nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['mcp_client.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspace.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'workspace_id')
    )
    op.create_index(op.f('ix_mcp_grant_client_id'), 'mcp_grant', ['client_id'], unique=False)
    op.create_index(op.f('ix_mcp_grant_workspace_id'), 'mcp_grant', ['workspace_id'], unique=False)

    op.create_table('mcp_usage',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('client_id', sa.String(length=40), nullable=False),
    sa.Column('workspace_id', sa.String(length=40), nullable=False),
    sa.Column('tool', sa.String(length=80), nullable=False),
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['mcp_client.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id', 'workspace_id', 'tool', 'day')
    )
    op.create_index(op.f('ix_mcp_usage_client_id'), 'mcp_usage', ['client_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_mcp_usage_client_id'), table_name='mcp_usage')
    op.drop_table('mcp_usage')
    op.drop_index(op.f('ix_mcp_grant_workspace_id'), table_name='mcp_grant')
    op.drop_index(op.f('ix_mcp_grant_client_id'), table_name='mcp_grant')
    op.drop_table('mcp_grant')
    op.drop_table('mcp_client')
    op.drop_index(op.f('ix_mcp_server_workspace_id'), table_name='mcp_server')
    op.drop_table('mcp_server')
