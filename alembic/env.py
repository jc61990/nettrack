"""
Alembic environment — wired to NetTrack's SQLAlchemy models.

- Reads DATABASE_URL from the environment (same as the app).
- Uses 'autogenerate' so `alembic revision --autogenerate` diffs your
  models against the live schema and writes the migration for you.
- Runs migrations in a single transaction where possible.
"""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Make sure the project root is on the path so we can import models
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import Base   # noqa: E402 — must come after sys.path insert
import models               # noqa: F401 — registers all models with Base.metadata

# Alembic Config object (gives access to alembic.ini values)
config = context.config

# Wire Python logging to alembic.ini settings
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata object autogenerate compares against
target_metadata = Base.metadata

# Read DATABASE_URL from environment — never hardcode credentials here
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Run: export DATABASE_URL=postgresql://user:pass@host/dbname"
    )
config.set_main_option("sqlalchemy.url", DATABASE_URL)


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — generates SQL without a live connection.
    Useful for generating migration scripts to review before applying.

        alembic upgrade head --sql > migration.sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode — applies them directly to the database.
    This is what `alembic upgrade head` uses.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,   # no connection pooling during migrations
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,              # detect column type changes
            compare_server_default=True,    # detect default value changes
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
