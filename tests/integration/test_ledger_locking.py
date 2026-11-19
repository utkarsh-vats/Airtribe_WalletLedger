"""lock_accounts against real Postgres, as wallet_app, across concurrent sessions.

The wallets are committed (other sessions must see them to contend for their locks) and deleted
afterwards as wallet_owner. No transfers or entries are created, so nothing append-only is left.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import transaction
from app.ledger.locking import AccountNotLockedError, lock_accounts

LOCK_NOT_AVAILABLE = "55P03"
TEST_EMAIL = "locking@ledger-tests.example.com"
WAIT = 5  # seconds: generous upper bound for anything that should happen promptly


@dataclass
class Wallets:
    a: int
    b: int
    c: int  # a < b < c


@pytest.fixture
async def wallets(owner_engine: AsyncEngine) -> AsyncIterator[Wallets]:
    async with owner_engine.begin() as owner:
        user_id = await owner.scalar(
            text(
                "INSERT INTO users (name, email, phone, password_hash) "
                "VALUES ('Locking', :e, '+919800000525', 'x') RETURNING id"
            ),
            {"e": TEST_EMAIL},
        )
        ids = (
            await owner.execute(
                text(
                    "INSERT INTO accounts (user_id, account_type, label) "
                    "SELECT :u, 'user_wallet', l FROM unnest(ARRAY['a', 'b', 'c']) AS l "
                    "RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalars()
    try:
        yield Wallets(*sorted(ids))
    finally:
        async with owner_engine.begin() as owner:
            await owner.execute(
                text(
                    "DELETE FROM accounts WHERE user_id = (SELECT id FROM users WHERE email = :e)"
                ),
                {"e": TEST_EMAIL},
            )
            await owner.execute(text("DELETE FROM users WHERE email = :e"), {"e": TEST_EMAIL})


async def _try_lock_nowait(engine: AsyncEngine, account_id: int) -> bool:
    """True if another session could lock the row right now, False if it is locked."""
    async with engine.connect() as other, other.begin():
        try:
            await other.execute(
                text("SELECT 1 FROM accounts WHERE id = :id FOR UPDATE NOWAIT"), {"id": account_id}
            )
        except DBAPIError as exc:
            assert getattr(exc.orig.__cause__, "sqlstate", None) == LOCK_NOT_AVAILABLE
            return False
        return True


async def _wait_until_blocked(engine: AsyncEngine, pid: int) -> None:
    """Wait until backend `pid` is waiting on a lock held by another session."""
    async with engine.connect() as probe:
        for _ in range(WAIT * 20):
            blocked = await probe.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            )
            await probe.rollback()
            if blocked:
                return
            await asyncio.sleep(0.05)
    raise AssertionError(f"backend {pid} never blocked")


async def _pid(conn: AsyncConnection) -> int:
    """The connection's backend pid, leaving no transaction open (the query autobegins one)."""
    pid = await conn.scalar(text("SELECT pg_backend_pid()"))
    await conn.rollback()
    return pid


async def test_returns_rows_in_id_order_and_reports_missing(
    conn: AsyncConnection, wallets: Wallets
) -> None:
    missing = 9_000_000_000_000

    async with transaction(conn):
        locked = await lock_accounts(conn, [wallets.c, wallets.a, missing, wallets.a])

    assert list(locked) == [wallets.a, wallets.c]
    assert locked.requested == (wallets.a, wallets.c, missing)
    assert locked.missing == {missing}
    a = locked[wallets.a]
    assert (a.account_type, a.currency, a.balance, a.held_amount) == ("user_wallet", "INR", 0, 0)


async def test_locks_system_accounts_too(conn: AsyncConnection) -> None:
    async with transaction(conn):
        locked = await lock_accounts(conn, [3, 1, 2])

    assert [locked[i].account_type for i in locked] == [
        "system_cash_in",
        "system_cash_out",
        "system_fee_revenue",
    ]
    assert all(locked[i].user_id is None for i in locked)


async def test_refuses_to_run_outside_a_transaction(conn: AsyncConnection) -> None:
    with pytest.raises(RuntimeError, match="inside a transaction"):
        await lock_accounts(conn, [1])


async def test_rows_stay_locked_until_the_transaction_ends(
    engine: AsyncEngine, conn: AsyncConnection, wallets: Wallets
) -> None:
    async with transaction(conn):
        await lock_accounts(conn, [wallets.a])

        assert not await _try_lock_nowait(engine, wallets.a)
        assert await _try_lock_nowait(engine, wallets.b)  # only the requested rows

    assert await _try_lock_nowait(engine, wallets.a)  # released at COMMIT


async def test_locks_in_ascending_order_whatever_the_input_order(
    engine: AsyncEngine, wallets: Wallets
) -> None:
    async with engine.connect() as first, engine.connect() as second:
        async with transaction(first):
            await lock_accounts(first, [wallets.c])  # holds the higher id

            async def lock_c_then_a() -> None:
                async with transaction(second):
                    await lock_accounts(second, [wallets.c, wallets.a])

            second_pid = await _pid(second)
            task = asyncio.create_task(lock_c_then_a())
            await _wait_until_blocked(engine, second_pid)

            # Listed c first, but it locked a first and is now waiting on c, holding a.
            assert not await _try_lock_nowait(engine, wallets.a)

        await asyncio.wait_for(task, WAIT)  # first committed; second gets c and finishes


async def test_opposite_orders_cannot_deadlock(engine: AsyncEngine, wallets: Wallets) -> None:
    """The classic deadlock: T1 holds a and then wants b; T2 asks for b and a.

    If T2 locked b first, T1's request for b would wait on T2 while T2 waited on T1 for a.
    lock_accounts makes T2 wait on a, holding nothing, so T1 gets b at once.
    """
    async with engine.connect() as t1, engine.connect() as t2:
        async with transaction(t1):
            await lock_accounts(t1, [wallets.a])

            async def t2_locks_b_and_a() -> None:
                async with transaction(t2):
                    await lock_accounts(t2, [wallets.b, wallets.a])

            t2_pid = await _pid(t2)
            task = asyncio.create_task(t2_locks_b_and_a())
            await _wait_until_blocked(engine, t2_pid)

            # No cycle: b was free, so this returns at once instead of deadlocking.
            await asyncio.wait_for(lock_accounts(t1, [wallets.b]), WAIT)

        await asyncio.wait_for(task, WAIT)


async def test_require_accepts_only_this_transactions_locks(
    conn: AsyncConnection, wallets: Wallets
) -> None:
    async with transaction(conn):
        locked = await lock_accounts(conn, [wallets.a])

        assert locked.require(conn, wallets.a).id == wallets.a
        with pytest.raises(AccountNotLockedError, match=f"account {wallets.b} is not locked"):
            locked.require(conn, wallets.b)

    async with transaction(conn):  # a new transaction: the old locks are gone
        with pytest.raises(AccountNotLockedError, match="different transaction"):
            locked.require(conn, wallets.a)
