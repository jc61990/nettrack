"""add scan_schedule and alert_config tables

Revision ID: 0002
Revises: 0001
Create Date: 2024-01-02 00:00:00.000000 UTC
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── scan_schedule ─────────────────────────────────────────────────────────
    op.create_table(
        'scan_schedule',
        sa.Column('id',               sa.Integer(),     nullable=False),
        sa.Column('enabled',          sa.Boolean(),     nullable=True, server_default='false'),
        sa.Column('preset',           sa.String(32),    nullable=True, server_default='every_4h'),
        sa.Column('last_run_at',      sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_run_status',  sa.String(32),    nullable=True),
        sa.Column('last_run_found',   sa.Integer(),     nullable=True),
        sa.Column('last_run_new',     sa.Integer(),     nullable=True),
        sa.Column('last_run_offline', sa.Integer(),     nullable=True),
        sa.Column('updated_at',       sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # Seed the singleton row
    op.execute("INSERT INTO scan_schedule (id, enabled, preset) VALUES (1, false, 'every_4h')")

    # ── alert_config ──────────────────────────────────────────────────────────
    op.create_table(
        'alert_config',
        sa.Column('id',                  sa.Integer(),     nullable=False),
        sa.Column('enabled',             sa.Boolean(),     nullable=True, server_default='false'),
        sa.Column('smtp_host',           sa.String(255),   nullable=True),
        sa.Column('smtp_port',           sa.Integer(),     nullable=True, server_default='587'),
        sa.Column('smtp_username',       sa.String(255),   nullable=True),
        sa.Column('smtp_password',       sa.String(255),   nullable=True),
        sa.Column('smtp_use_tls',        sa.Boolean(),     nullable=True, server_default='true'),
        sa.Column('from_address',        sa.String(255),   nullable=True),
        sa.Column('recipients',          sa.Text(),        nullable=True),
        sa.Column('alert_on_complete',   sa.Boolean(),     nullable=True, server_default='false'),
        sa.Column('alert_on_new_device', sa.Boolean(),     nullable=True, server_default='true'),
        sa.Column('alert_on_offline',    sa.Boolean(),     nullable=True, server_default='true'),
        sa.Column('updated_at',          sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # Seed the singleton row
    op.execute("INSERT INTO alert_config (id, enabled) VALUES (1, false)")


def downgrade() -> None:
    op.drop_table('alert_config')
    op.drop_table('scan_schedule')
