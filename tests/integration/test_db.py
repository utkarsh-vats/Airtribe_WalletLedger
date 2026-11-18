from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import set_lock_timeout, transaction


async def _scalar(conn: AsyncConnection, sql: str) -> object:
    return (await conn.execute(text(sql))).scalar_one()


# A fixed instant, not the wall clock: the commit test only needs a value it can recognise.
_STAMP = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)
_JOB = "flag_explanations"


@pytest.fixture
async def job_lock_row(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """`conn`, with the job_locks row's last_started_at restored after the test.

    job_locks is the one table where a committed UPDATE adds no rows and touches nothing
    append-only, so the commit test can really commit and still leave the database as it was.
    """
    async with transaction(conn):
        before = await conn.scalar(
            text("SELECT last_started_at FROM job_locks WHERE job_name = :j"), {"j": _JOB}
        )
    yield conn
    if conn.in_transaction():
        await conn.rollback()
    async with transaction(conn):
        await conn.execute(
            text("UPDATE job_locks SET last_started_at = :v WHERE job_name = :j"),
            {"v": before, "j": _JOB},
        )


async def test_commit_persists(job_lock_row: AsyncConnection, engine: AsyncEngine) -> None:
    conn = job_lock_row
    async with transaction(conn):
        await conn.execute(
            text("UPDATE job_locks SET last_started_at = :v WHERE job_name = :j"),
            {"v": _STAMP, "j": _JOB},
        )

    assert not conn.in_transaction()
    async with engine.connect() as other, transaction(other):  # visible to another session
        stored = await other.scalar(
            text("SELECT last_started_at FROM job_locks WHERE job_name = :j"), {"j": _JOB}
        )
    assert stored == _STAMP


async def test_exception_rolls_back(conn: AsyncConnection) -> None:
    with pytest.raises(ValueError, match="boom"):
        async with transaction(conn):
            await conn.execute(
                text(
                    "INSERT INTO users (name, email, phone, password_hash) "
                    "VALUES ('T', 'rollback@example.test', '+919877777777', 'x')"
                )
            )
            raise ValueError("boom")

    assert not conn.in_transaction()
    async with transaction(conn):
        count = await conn.scalar(
            text("SELECT count(*) FROM users WHERE email = 'rollback@example.test'")
        )
    assert count == 0


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


async def test_connects_to_test_database_as_runtime_role(conn: AsyncConnection) -> None:
    async with transaction(conn):
        assert await _scalar(conn, "SELECT current_database()") == "walletledger_test"
        assert await _scalar(conn, "SELECT current_user") == "wallet_app"


async def test_runtime_role_cannot_create_tables(conn: AsyncConnection) -> None:
    """Schema changes belong to wallet_owner via migrations; wallet_app only gets table grants."""
    with pytest.raises(DBAPIError) as exc_info:
        async with transaction(conn):
            await conn.execute(text("CREATE TABLE public.should_fail (x int)"))

    assert exc_info.value.orig.sqlstate == "42501"  # type: ignore[union-attr]  # insufficient_privilege


async def test_runtime_role_cannot_create_temporary_tables(conn: AsyncConnection) -> None:
    """TEMPORARY was revoked in 0008: wallet_app can't create any object, not even a temp table."""
    with pytest.raises(DBAPIError) as exc_info:
        async with transaction(conn):
            await conn.execute(text("CREATE TEMP TABLE should_fail (x int)"))

    assert exc_info.value.orig.sqlstate == "42501"  # type: ignore[union-attr]  # insufficient_privilege


async def test_runtime_role_has_only_connect_on_the_database(conn: AsyncConnection) -> None:
    async with transaction(conn):
        privileges = (
            await conn.execute(
                text(
                    "SELECT has_database_privilege(current_database(), 'CONNECT'), "
                    "has_database_privilege(current_database(), 'TEMPORARY'), "
                    "has_database_privilege(current_database(), 'CREATE')"
                )
            )
        ).one()
    assert tuple(privileges) == (True, False, False)
