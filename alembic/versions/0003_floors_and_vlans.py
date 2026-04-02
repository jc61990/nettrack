"""add floors and vlans tables

Revision ID: 0003
Revises: 0002
Create Date: 2024-01-03 00:00:00.000000 UTC
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── floors ────────────────────────────────────────────────────────────────
    op.create_table(
        'floors',
        sa.Column('id',         sa.Integer(),    nullable=False),
        sa.Column('name',       sa.String(64),   nullable=False),
        sa.Column('building',   sa.String(128),  nullable=True),
        sa.Column('sort_order', sa.Integer(),    nullable=True, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_floors_id', 'floors', ['id'], unique=False)

    # ── vlans ─────────────────────────────────────────────────────────────────
    op.create_table(
        'vlans',
        sa.Column('id',          sa.Integer(),  nullable=False),
        sa.Column('vlan_id',     sa.Integer(),  nullable=False),
        sa.Column('name',        sa.String(64), nullable=True),
        sa.Column('description', sa.Text(),     nullable=True),
        sa.Column('created_at',  sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_vlans_id',      'vlans', ['id'],      unique=False)
    op.create_index('ix_vlans_vlan_id', 'vlans', ['vlan_id'], unique=True)


def downgrade() -> None:
    op.drop_table('vlans')
    op.drop_table('floors')
