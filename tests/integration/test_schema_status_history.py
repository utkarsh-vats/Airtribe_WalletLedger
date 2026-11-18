"""Migration 0006: status-history trigger (DESIGN.md §3.3).

Runs as wallet_app in rolled-back transactions. wallet_app can't insert history rows itself
(0004), so every row seen here was written by the trigger.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import set_actor, set_status_reason


@pytest.fixture
async def app_tx(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()


async def _user_and_wallets(conn: AsyncConnection) -> tuple[int, int, int]:
    user_id = await conn.scalar(
        text(
            "INSERT INTO users (name, email, phone, password_hash) "
            "VALUES ('H', 'h@example.test', '+919833333333', 'x') RETURNING id"
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
    return user_id, a, b


async def _held_transfer(conn: AsyncConnection, user_id: int, a: int, b: int) -> int:
    return await conn.scalar(
        text(
            "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, "
            "initiated_by) VALUES ('peer', 'held', 500, :f, :t, :u) RETURNING id"
        ),
        {"f": a, "t": b, "u": user_id},
    )


async def _history(conn: AsyncConnection, transfer_id: int) -> list[tuple]:
    rows = await conn.execute(
        text(
            "SELECT from_status, to_status, changed_by, reason FROM transfer_status_history "
            "WHERE transfer_id = :x ORDER BY id"
        ),
        {"x": transfer_id},
    )
    return [tuple(r) for r in rows]


async def test_insert_records_initial_status_with_actor(app_tx: AsyncConnection) -> None:
    user_id, a, b = await _user_and_wallets(app_tx)
    await set_actor(app_tx, user_id)

    x = await _held_transfer(app_tx, user_id, a, b)

    assert await _history(app_tx, x) == [(None, "held", user_id, None)]


async def test_no_actor_means_system(app_tx: AsyncConnection) -> None:
    user_id, a, b = await _user_and_wallets(app_tx)

    x = await _held_transfer(app_tx, user_id, a, b)

    assert await _history(app_tx, x) == [(None, "held", None, None)]


async def test_status_change_records_transition_actor_and_reason(app_tx: AsyncConnection) -> None:
    user_id, a, b = await _user_and_wallets(app_tx)
    x = await _held_transfer(app_tx, user_id, a, b)

    await set_actor(app_tx, user_id)
    await set_status_reason(app_tx, "looks fine")
    await app_tx.execute(
        text("UPDATE transfers SET status = 'rejected', updated_at = now() WHERE id = :x"),
        {"x": x},
    )

    assert await _history(app_tx, x) == [
        (None, "held", None, None),
        ("held", "rejected", user_id, "looks fine"),
    ]


async def test_cleared_reason_is_null(app_tx: AsyncConnection) -> None:
    user_id, a, b = await _user_and_wallets(app_tx)
    await set_status_reason(app_tx, "first")
    await set_status_reason(app_tx, None)

    x = await _held_transfer(app_tx, user_id, a, b)

    assert await _history(app_tx, x) == [(None, "held", None, None)]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE transfers SET status = 'held' WHERE id = :x",  # same value
        "UPDATE transfers SET updated_at = now() WHERE id = :x",  # other column
    ],
)
async def test_no_row_without_a_real_status_change(app_tx: AsyncConnection, statement: str) -> None:
    user_id, a, b = await _user_and_wallets(app_tx)
    x = await _held_transfer(app_tx, user_id, a, b)

    await app_tx.execute(text(statement), {"x": x})

    assert len(await _history(app_tx, x)) == 1


async def test_function_is_locked_down(app_tx: AsyncConnection) -> None:
    owner, security_definer, config, app_can_execute = (
        await app_tx.execute(
            text(
                "SELECT pg_get_userbyid(proowner), prosecdef, proconfig, "
                "has_function_privilege('wallet_app', oid, 'EXECUTE') "
                "FROM pg_proc WHERE proname = 'record_transfer_status_change'"
            )
        )
    ).one()

    assert owner == "wallet_owner"
    assert security_definer is True
    assert config == ["search_path=pg_catalog, public"]
    assert app_can_execute is False
