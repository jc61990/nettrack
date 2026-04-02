"""add discovery_queue table

Revision ID: 0004
Revises: 0003
Create Date: 2024-01-04 00:00:00.000000 UTC
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'discovery_queue',
        sa.Column('id',            sa.Integer(),     nullable=False),
        sa.Column('hostname',      sa.String(255),   nullable=True),
        sa.Column('ip',            sa.String(45),    nullable=True),
        sa.Column('mac',           sa.String(17),    nullable=True),
        sa.Column('type',          sa.String(64),    nullable=True),
        sa.Column('status',        sa.String(32),    nullable=True),
        sa.Column('floor',         sa.String(16),    nullable=True),
        sa.Column('location',      sa.String(255),   nullable=True),
        sa.Column('switch',        sa.String(64),    nullable=True),
        sa.Column('port',          sa.String(32),    nullable=True),
        sa.Column('vlan',          sa.Integer(),     nullable=True),
        sa.Column('notes',         sa.Text(),        nullable=True),
        sa.Column('queue_state',   sa.String(16),    nullable=False),
        sa.Column('queue_status',  sa.String(16),    nullable=False, server_default='pending'),
        sa.Column('diff',          sa.Text(),        nullable=True),
        sa.Column('scan_id',       sa.String(64),    nullable=True),
        sa.Column('existing_id',   sa.Integer(),     nullable=True),
        sa.Column('discovered_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('reviewed_at',   sa.DateTime(timezone=True), nullable=True),
        sa.Column('reviewed_by',   sa.Integer(),     nullable=True),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_discovery_queue_id',           'discovery_queue', ['id'],           unique=False)
    op.create_index('ix_discovery_queue_mac',          'discovery_queue', ['mac'],          unique=False)
    op.create_index('ix_discovery_queue_queue_state',  'discovery_queue', ['queue_state'],  unique=False)
    op.create_index('ix_discovery_queue_queue_status', 'discovery_queue', ['queue_status'], unique=False)
    op.create_index('ix_discovery_queue_discovered_at','discovery_queue', ['discovered_at'],unique=False)


def downgrade() -> None:
    op.drop_table('discovery_queue')
