"""
alembic/env.py — Alembic environment for xREF DataMapper.

Reads DATABASE_URL from the environment (or the resolver in app.core.db)
so the same logic that drives runtime also drives migrations.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the project root importable so we can pull in app.models
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env so DATABASE_URL is picked up when running alembic from a shell
try:
    from dotenv import load_dotenv
    _env = _PROJECT_ROOT / ".env"
    if _env.exists():
        load_dotenv(_env, override=False)
except Exception:
    pass

from app.models.platform import Base  # noqa: E402
from app.core.db import _resolve_database_url  # noqa: E402

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

# Inject the resolved database URL.
_url = os.getenv("DATABASE_URL") or _resolve_database_url() or ""
if _url.startswith("postgres://"):
    _url = "postgresql+psycopg2://" + _url[len("postgres://"):]
config.set_main_option("sqlalchemy.url", _url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
