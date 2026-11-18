from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import set_lock_timeout, transaction


async def _scalar(conn: AsyncConnection, sql: str) -> object:
    return (await conn.execute(text(sql))).scalar_one()


@pytest.fixture
async def scratch(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """`conn` with a session-private temp table, committed so tests start with no tx open.

    Dropped afterwards: the pooled connection (and its temp tables) outlives the test.
    """
    await conn.execute(text("CREATE TEMP TABLE scratch (x int NOT NULL)"))
    await conn.commit()
    yield conn
    if conn.in_transaction():
        await conn.rollback()
    await conn.execute(text("DROP TABLE scratch"))
    await conn.commit()


async def test_commit_persists(scratch: AsyncConnection) -> None:
    async with transaction(scratch):
        await scratch.execute(text("INSERT INTO scratch VALUES (1)"))

    assert not scratch.in_transaction()
    async with transaction(scratch):
        assert await _scalar(scratch, "SELECT count(*) FROM scratch") == 1


async def test_exception_rolls_back(scratch: AsyncConnection) -> None:
    with pytest.raises(ValueError, match="boom"):
        async with transaction(scratch):
            await scratch.execute(text("INSERT INTO scratch VALUES (1)"))
            raise ValueError("boom")

    async with transaction(scratch):
        assert await _scalar(scratch, "SELECT count(*) FROM scratch") == 0


async def test_actor_is_transaction_local(conn: AsyncConnection) -> None:
    async with transaction(conn, actor_id=42):
        assert await _scalar(conn, "SELECT current_setting('app.actor_id', true)") == "42"

    async with transaction(conn):
        # Once a custom setting has been defined in the session, it reads back as '' (not NULL).
        assert await _scalar(conn, "SELECT current_setting('app.actor_id', true)") in ("", None)


async def test_lock_timeout_is_transaction_local_and_switchable(conn: AsyncConnection) -> None:
    async with transaction(conn, lock_timeout="2s"):
        assert await _scalar(conn, "SHOW lock_timeout") == "2s"
        await set_lock_timeout(conn, "0")
        assert await _scalar(conn, "SHOW lock_timeout") == "0"
        await set_lock_timeout(conn, "2s")

    async with transaction(conn):
        assert await _scalar(conn, "SHOW lock_timeout") == "0"


@pytest.mark.parametrize("level", ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"])
async def test_isolation_level_applied(conn: AsyncConnection, level: str) -> None:
    async with transaction(conn, isolation=level):  # type: ignore[arg-type]
        assert await _scalar(conn, "SHOW transaction_isolation") == level.lower()


async def test_refuses_when_transaction_already_open(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT 1"))  # SQLAlchemy autobegins here
    assert conn.in_transaction()

    with pytest.raises(RuntimeError, match="already in a transaction"):
        async with transaction(conn):
            pass
    await conn.rollback()


async def test_lock_timeout_aborts_a_blocked_wait(engine: AsyncEngine) -> None:
    """The mechanism §5 relies on: a waiter gives up with SQLSTATE 55P03 after lock_timeout."""
    async with engine.connect() as holder, engine.connect() as waiter:
        async with transaction(holder):
            await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 7_000_001})

            with pytest.raises(DBAPIError) as exc_info:
                async with transaction(waiter, lock_timeout="200ms"):
                    await waiter.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 7_000_001})

    assert exc_info.value.orig.sqlstate == "55P03"  # type: ignore[union-attr]
