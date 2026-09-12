from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.base import Base
from app.db import init_db as _model_registry  # noqa: F401

try:
    from sqlmodel import SQLModel
except ModuleNotFoundError:  # pragma: no cover
    SQLModel = None

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = str(settings.SQLALCHEMY_DATABASE_URI)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

if SQLModel is not None:
    target_metadata = [Base.metadata, SQLModel.metadata]
else:
    target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _bootstrap_empty_database(connection) -> bool:
    """Materialize the v0.1 Alpha baseline only when the database is empty."""
    existing = set(inspect(connection).get_table_names()) - {"alembic_version"}
    if existing:
        return False
    Base.metadata.create_all(connection)
    if SQLModel is not None:
        SQLModel.metadata.create_all(connection)
    return True


def _run_sync_migrations(connection) -> None:
    _bootstrap_empty_database(connection)
    # Both metadata.create_all() and schema inspection can start an implicit
    # SQLAlchemy transaction. Finish that probe/bootstrap transaction before
    # Alembic opens its own migration transaction; otherwise SQLite can retain
    # DDL while rolling back the alembic_version row on connection close.
    if connection.in_transaction():
        connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
