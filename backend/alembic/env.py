"""Alembic environment.

Runs migrations through the application's own async engine configuration so
there is exactly one source of truth for the database URL: the environment.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from app.core.config import settings
from app.core.database import _ensure_models_registered

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

Base = _ensure_models_registered()
target_metadata = Base.metadata

DB_SCHEMA = settings.DB_SCHEMA


def _url() -> str:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set; cannot run migrations")
    return settings.DATABASE_URL


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=DB_SCHEMA,
        include_schemas=True,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=DB_SCHEMA,
        include_schemas=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {DB_SCHEMA}")
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _url())
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
        # SQLAlchemy 2.x AsyncConnection rolls back on context exit unless the
        # transaction is committed explicitly. Without this the migration logs
        # a successful upgrade and silently leaves the database untouched.
        await connection.commit()
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
