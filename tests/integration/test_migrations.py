import os
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.config import get_settings
from app.db import transaction


async def test_test_database_is_at_head_and_owned_by_schema_owner() -> None:
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    owner_engine = create_async_engine(get_settings().migration_database_url)  # test DB
    try:
        async with owner_engine.connect() as conn, transaction(conn):
            assert await conn.scalar(text("SELECT current_database()")) == "walletledger_test"
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            owner = await conn.scalar(
                text("SELECT tableowner FROM pg_tables WHERE tablename = 'alembic_version'")
            )
    finally:
        await owner_engine.dispose()

    assert version == head
    assert owner == "wallet_owner"


async def test_runtime_role_has_no_access_to_alembic_version(conn: AsyncConnection) -> None:
    """Least privilege: wallet_app only gets tables a migration explicitly grants."""
    with pytest.raises(DBAPIError) as exc_info:
        async with transaction(conn):
            await conn.execute(text("SELECT version_num FROM alembic_version"))

    assert exc_info.value.orig.sqlstate == "42501"  # type: ignore[union-attr]


def test_env_refuses_to_migrate_as_runtime_role() -> None:
    """Running migrations as wallet_app would create objects with the wrong owner."""
    env = {**os.environ, "MIGRATION_DATABASE_URL": os.environ["DATABASE_URL"]}  # wallet_app
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "migrations must run as 'wallet_owner'" in result.stderr
