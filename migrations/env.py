"""Alembic environment: async, hand-written SQL, run as the schema-owner role.

- URL comes from app settings: MIGRATION_DATABASE_URL by default, TEST_MIGRATION_DATABASE_URL
  with `alembic -x target=test ...`. Nothing is read from alembic.ini.
- No target_metadata: there is no ORM model to autogenerate from. Every migration is SQL.
- Refuses to run unless connected as `wallet_owner`, so every object is owned by that role and
  the runtime role `wallet_app` only ever gets explicit grants.
- One transaction per migration: a failing migration rolls back cleanly on its own.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings

SCHEMA_OWNER_ROLE = "wallet_owner"

if context.config.config_file_name is not None:
    # Keep loggers configured by the host process (e.g. pytest, the app) working.
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    settings = get_settings()
    target = context.get_x_argument(as_dictionary=True).get("target", "dev")
    if target == "dev":
        return settings.migration_database_url
    if target == "test":
        return settings.test_migration_database_url
    raise SystemExit(f"unknown -x target={target!r}; use 'dev' or 'test'")


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    context.configure(url=_database_url(), literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    role = connection.execute(text("SELECT current_user")).scalar_one()
    if role != SCHEMA_OWNER_ROLE:
        raise SystemExit(
            f"migrations must run as {SCHEMA_OWNER_ROLE!r}, connected as {role!r}; "
            "check MIGRATION_DATABASE_URL"
        )
    # The check above autobegan a transaction; end it so each migration gets its own.
    connection.commit()

    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
