"""Migration 0005: deferred per-transfer balance check (DESIGN.md §3.3, schema.md §4 Q4).

Runs as wallet_app. Entries are append-only, so tests must not commit rows: each case forces
the deferred check with `SET CONSTRAINTS ALL IMMEDIATE` inside a transaction that is rolled
back. One test does a real COMMIT of an unbalanced transfer, which fails and stores nothing.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

CHECK_VIOLATION = "23514"


@dataclass
class Wallets:
    user_id: int
    a: int
    b: int


@pytest.fixture
async def app_tx(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()


async def _wallets(conn: AsyncConnection) -> Wallets:
    user_id = await conn.scalar(
        text(
            "INSERT INTO users (name, email, phone, password_hash) "
            "VALUES ('B', 'b@example.test', '+919822222222', 'x') RETURNING id"
        )
    )
    a, b = (
        await conn.execute(
            text(
                "INSERT INTO accounts (user_id, account_type, label) "
                "VALUES (:u, 'user_wallet', 'main'), (:u, 'user_wallet', 'savings') RETURNING id"
            ),
            {"u": user_id},
        )
    ).scalars()
    return Wallets(user_id, a, b)


async def _transfer(conn: AsyncConnection, w: Wallets, status: str, amount: int = 500) -> int:
    return await conn.scalar(
        text(
            "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, "
            "initiated_by) VALUES ('peer', :s, :amt, :f, :t, :u) RETURNING id"
        ),
        {"s": status, "amt": amount, "f": w.a, "t": w.b, "u": w.user_id},
    )


async def _entries(
    conn: AsyncConnection, transfer_id: int, legs: list[tuple[int, str, int]]
) -> None:
    for account_id, direction, amount in legs:
        await conn.execute(
            text(
                "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
                "VALUES (:x, :a, :d, :amt)"
            ),
            {"x": transfer_id, "a": account_id, "d": direction, "amt": amount},
        )


async def _check_now(conn: AsyncConnection) -> None:
    """Run every pending deferred constraint check now, as COMMIT would."""
    await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


async def _violation(conn: AsyncConnection) -> str:
    """Force the deferred check and return the check_violation message."""
    with pytest.raises(DBAPIError) as exc_info:
        await _check_now(conn)
    assert exc_info.value.orig.sqlstate == CHECK_VIOLATION  # type: ignore[union-attr]
    return str(exc_info.value.orig)


# --- (a) completed / reversed: entries present, Σ debits = Σ credits = amount ---


async def test_balanced_two_leg_transfer_passes(app_tx: AsyncConnection) -> None:
    w = await _wallets(app_tx)
    x = await _transfer(app_tx, w, "completed")
    await _entries(app_tx, x, [(w.a, "debit", 500), (w.b, "credit", 500)])

    await _check_now(app_tx)


async def test_multi_leg_transfer_passes_no_entry_count_limit(app_tx: AsyncConnection) -> None:
    w = await _wallets(app_tx)
    x = await _transfer(app_tx, w, "completed")
    await _entries(
        app_tx,
        x,
        [(w.a, "debit", 300), (w.a, "debit", 200), (w.b, "credit", 400), (w.b, "credit", 100)],
    )

    await _check_now(app_tx)


async def test_completed_without_entries_fails(app_tx: AsyncConnection) -> None:
    w = await _wallets(app_tx)
    x = await _transfer(app_tx, w, "completed")

    message = await _violation(app_tx)
    assert f"transfer {x} (completed) is unbalanced" in message
    assert (
        "expected debits = credits = 500, got debits = 0, credits = 0 across 0 entries" in message
    )


async def test_balanced_but_not_equal_to_amount_fails(app_tx: AsyncConnection) -> None:
    """Q4: debits = credits is not enough; each must equal transfers.amount."""
    w = await _wallets(app_tx)
    x = await _transfer(app_tx, w, "completed", amount=100)
    await _entries(app_tx, x, [(w.a, "debit", 200), (w.b, "credit", 200)])

    message = await _violation(app_tx)
    assert (
        "expected debits = credits = 100, got debits = 200, credits = 200 across 2 entries"
        in message
    )


async def test_debits_not_equal_credits_fails(app_tx: AsyncConnection) -> None:
    w = await _wallets(app_tx)
    x = await _transfer(app_tx, w, "completed")
    await _entries(app_tx, x, [(w.a, "debit", 500), (w.b, "credit", 499)])

    message = await _violation(app_tx)
    assert "got debits = 500, credits = 499" in message


async def test_reversed_original_with_balanced_entries_passes(app_tx: AsyncConnection) -> None:
    w = await _wallets(app_tx)
    x = await _transfer(app_tx, w, "completed")
    await _entries(app_tx, x, [(w.a, "debit", 500), (w.b, "credit", 500)])
    await app_tx.execute(text("UPDATE transfers SET status = 'reversed' WHERE id = :x"), {"x": x})

    await _check_now(app_tx)


# --- (b) held / rejected: zero entries ---


@pytest.mark.parametrize("status", ["held", "rejected"])
async def test_unsettled_without_entries_passes(app_tx: AsyncConnection, status: str) -> None:
    w = await _wallets(app_tx)
    await _transfer(app_tx, w, status)

    await _check_now(app_tx)


@pytest.mark.parametrize("status", ["held", "rejected"])
async def test_unsettled_with_entries_fails(app_tx: AsyncConnection, status: str) -> None:
    w = await _wallets(app_tx)
    x = await _transfer(app_tx, w, status)
    await _entries(app_tx, x, [(w.a, "debit", 500), (w.b, "credit", 500)])

    message = await _violation(app_tx)
    assert f"transfer {x} ({status}) must have no entries: expected 0, got 2" in message


# --- status changes are checked too ---


async def test_release_to_completed_without_entries_fails(app_tx: AsyncConnection) -> None:
    w = await _wallets(app_tx)
    x = await _transfer(app_tx, w, "held")
    await _check_now(app_tx)
    await app_tx.execute(text("SET CONSTRAINTS ALL DEFERRED"))

    await app_tx.execute(text("UPDATE transfers SET status = 'completed' WHERE id = :x"), {"x": x})

    message = await _violation(app_tx)
    assert f"transfer {x} (completed) is unbalanced" in message


async def test_release_with_entries_in_same_transaction_passes(app_tx: AsyncConnection) -> None:
    w = await _wallets(app_tx)
    x = await _transfer(app_tx, w, "held")
    await _check_now(app_tx)
    await app_tx.execute(text("SET CONSTRAINTS ALL DEFERRED"))

    await app_tx.execute(text("UPDATE transfers SET status = 'completed' WHERE id = :x"), {"x": x})
    await _entries(app_tx, x, [(w.a, "debit", 500), (w.b, "credit", 500)])

    await _check_now(app_tx)


# --- deferral: intermediate states are allowed, only COMMIT counts ---


async def test_check_is_deferred_until_commit(engine: AsyncEngine) -> None:
    """A real COMMIT: the half-written transfer is accepted statement by statement, then the
    commit fails with check_violation and nothing is stored."""
    async with engine.connect() as conn:
        await conn.begin()
        w = await _wallets(conn)
        x = await _transfer(conn, w, "completed")
        await _entries(conn, x, [(w.a, "debit", 500)])  # no error yet: the check is deferred

        with pytest.raises(DBAPIError) as exc_info:
            await conn.commit()
        assert exc_info.value.orig.sqlstate == CHECK_VIOLATION  # type: ignore[union-attr]

        await conn.rollback()
        async with conn.begin():
            stored = await conn.scalar(
                text("SELECT count(*) FROM transfers WHERE id = :x"), {"x": x}
            )
        assert stored == 0
