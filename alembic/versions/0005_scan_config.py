"""add scan_config table

Revision ID: 0005
Revises: 0004
Create Date: 2024-01-05 00:00:00.000000 UTC
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scan_config',
        sa.Column('id',              sa.Integer(),     nullable=False),
        sa.Column('subnets',         sa.Text(),        nullable=True),
        sa.Column('snmp_community',  sa.String(128),   nullable=True, server_default='public'),
        sa.Column('snmp_port',       sa.Integer(),     nullable=True, server_default='161'),
        sa.Column('snmp_timeout',    sa.Integer(),     nullable=True, server_default='2'),
        sa.Column('snmp_retries',    sa.Integer(),     nullable=True, server_default='1'),
        sa.Column('ping_timeout_ms', sa.Integer(),     nullable=True, server_default='800'),
        sa.Column('ping_workers',    sa.Integer(),     nullable=True, server_default='64'),
        sa.Column('updated_at',      sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # Seed singleton row with defaults
    op.execute("""
        INSERT INTO scan_config (id, subnets, snmp_community, snmp_port,
                                  snmp_timeout, snmp_retries, ping_timeout_ms, ping_workers)
        VALUES (1, '[]', 'public', 161, 2, 1, 800, 64)
    """)


def downgrade() -> None:
    op.drop_table('scan_config')
