"""
Alembic environment configuration.

Loads the app config for the database URL and makes ``Base.metadata``
available for auto-detection of schema changes.

All model classes are loaded from ``models`` so that Alembic can detect
any new tables, column changes, or index modifications automatically.
"""

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the project root importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_config  # noqa: E402

cfg = get_config()

# ── Model metadata for auto-detection ────────────────────────────────────────
from models import Base  # noqa: E402
target_metadata = Base.metadata

# ── Alembic config ─────────────────────────────────────────────────────────
alembic_config = context.config
alembic_config.set_main_option("sqlalchemy.url", cfg.DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DB connection)."""
    context.configure(
        url=cfg.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live database."""
    connectable = engine_from_config(
        alembic_config.get_section(alembic_config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
