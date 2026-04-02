"""expand mac column to varchar(32) to handle any MAC format

Revision ID: 0006
Revises: 0005
Create Date: 2024-01-06 00:00:00.000000 UTC
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Expand mac column on devices table
    op.alter_column('devices', 'mac',
        existing_type=sa.String(17),
        type_=sa.String(32),
        existing_nullable=True,
    )
    # Expand mac column on discovery_queue table
    op.alter_column('discovery_queue', 'mac',
        existing_type=sa.String(17),
        type_=sa.String(32),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column('devices', 'mac',
        existing_type=sa.String(32),
        type_=sa.String(17),
        existing_nullable=True,
    )
    op.alter_column('discovery_queue', 'mac',
        existing_type=sa.String(32),
        type_=sa.String(17),
        existing_nullable=True,
    )
