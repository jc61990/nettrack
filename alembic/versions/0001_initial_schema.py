"""initial schema

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000 UTC

Creates all four tables:
  - devices
  - switches
  - users
  - audit_log
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── devices ───────────────────────────────────────────────────────────────
    op.create_table(
        'devices',
        sa.Column('id',         sa.Integer(),     nullable=False),
        sa.Column('hostname',   sa.String(255),   nullable=True),
        sa.Column('ip',         sa.String(45),    nullable=True),
        sa.Column('mac',        sa.String(17),    nullable=True),
        sa.Column('type',       sa.String(64),    nullable=True),
        sa.Column('status',     sa.String(32),    nullable=True, server_default='Unknown'),
        sa.Column('floor',      sa.String(16),    nullable=True),
        sa.Column('location',   sa.String(255),   nullable=True),
        sa.Column('switch',     sa.String(64),    nullable=True),
        sa.Column('port',       sa.String(32),    nullable=True),
        sa.Column('vlan',       sa.Integer(),     nullable=True),
        sa.Column('notes',      sa.Text(),        nullable=True, server_default=''),
        sa.Column('last_seen',  sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_devices_id',       'devices', ['id'],       unique=False)
    op.create_index('ix_devices_hostname', 'devices', ['hostname'], unique=False)
    op.create_index('ix_devices_ip',       'devices', ['ip'],       unique=False)
    op.create_index('ix_devices_mac',      'devices', ['mac'],      unique=True)
    op.create_index('ix_devices_switch',   'devices', ['switch'],   unique=False)

    # ── switches ──────────────────────────────────────────────────────────────
    op.create_table(
        'switches',
        sa.Column('id',         sa.Integer(),     nullable=False),
        sa.Column('name',       sa.String(64),    nullable=True),
        sa.Column('ip',         sa.String(45),    nullable=True),
        sa.Column('location',   sa.String(255),   nullable=True),
        sa.Column('floor',      sa.String(16),    nullable=True),
        sa.Column('model',      sa.String(128),   nullable=True),
        sa.Column('notes',      sa.Text(),        nullable=True, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_switches_id',   'switches', ['id'],   unique=False)
    op.create_index('ix_switches_name', 'switches', ['name'], unique=True)

    # ── users ─────────────────────────────────────────────────────────────────
    op.create_table(
        'users',
        sa.Column('id',            sa.Integer(),     nullable=False),
        sa.Column('email',         sa.String(255),   nullable=False),
        sa.Column('full_name',     sa.String(255),   nullable=True),
        sa.Column('role',          sa.String(32),    nullable=True, server_default='read_only'),
        sa.Column('auth_provider', sa.String(32),    nullable=True, server_default='local'),
        sa.Column('password_hash', sa.String(255),   nullable=True),
        sa.Column('refresh_token', sa.Text(),        nullable=True),
        sa.Column('is_active',     sa.Boolean(),     nullable=True, server_default='true'),
        sa.Column('created_at',    sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('last_login',    sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_users_id',    'users', ['id'],    unique=False)
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    # ── audit_log ─────────────────────────────────────────────────────────────
    op.create_table(
        'audit_log',
        sa.Column('id',          sa.Integer(),   nullable=False),
        sa.Column('user_id',     sa.Integer(),   nullable=True),
        sa.Column('action',      sa.String(64),  nullable=True),
        sa.Column('resource',    sa.String(64),  nullable=True),
        sa.Column('resource_id', sa.Integer(),   nullable=True),
        sa.Column('detail',      sa.Text(),      nullable=True),
        sa.Column('created_at',  sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_log_id',         'audit_log', ['id'],         unique=False)
    op.create_index('ix_audit_log_action',     'audit_log', ['action'],     unique=False)
    op.create_index('ix_audit_log_resource',   'audit_log', ['resource'],   unique=False)
    op.create_index('ix_audit_log_created_at', 'audit_log', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_table('audit_log')
    op.drop_table('users')
    op.drop_table('switches')
    op.drop_table('devices')
