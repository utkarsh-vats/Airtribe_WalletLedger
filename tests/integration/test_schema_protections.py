"""Migration 0004: append-only triggers and wallet_app grants (DESIGN.md §3.3).

Two layers are tested separately:
- triggers reject UPDATE/DELETE/TRUNCATE on append-only tables even for wallet_owner;
- wallet_app is stopped earlier, by missing privileges (42501), and can do what services need.
Every test runs in a transaction that is rolled back.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

INSUFFICIENT_PRIVILEGE = "42501"
RAISE_EXCEPTION = "P0001"


@dataclass
class Ledger:
    user_id: int
    sender: int
    recipient: int
    transfer_id: int


async def _seed(conn: AsyncConnection, *, with_history: bool) -> Ledger:
    """User, two wallets and one settled peer transfer with its two entries."""
    user_id = await conn.scalar(
        text(
            "INSERT INTO users (name, email, phone, password_hash) "
            "VALUES ('P', 'p@example.test', '+919811111111', 'x') RETURNING id"
        )
    )
    sender, recipient = (
        await conn.execute(
            text(
                "INSERT INTO accounts (user_id, account_type, label) "
                "VALUES (:u, 'user_wallet', 'main'), (:u, 'user_wallet', 'savings') RETURNING id"
            ),
            {"u": user_id},
        )
    ).scalars()
    transfer_id = await conn.scalar(
        text(
            "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, "
            "initiated_by) VALUES ('peer', 'completed', 500, :f, :t, :u) RETURNING id"
        ),
        {"f": sender, "t": recipient, "u": user_id},
    )
    await conn.execute(
        text(
            "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
            "VALUES (:x, :f, 'debit', 500), (:x, :t, 'credit', 500)"
        ),
        {"x": transfer_id, "f": sender, "t": recipient},
    )
    if with_history:
        await conn.execute(
            text(
                "INSERT INTO transfer_status_history (transfer_id, to_status, changed_by) "
                "VALUES (:x, 'completed', :u)"
            ),
            {"x": transfer_id, "u": user_id},
        )
    return Ledger(user_id, sender, recipient, transfer_id)


@pytest.fixture
async def app_tx(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """wallet_app connection inside a transaction that is always rolled back."""
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()


async def _sqlstate(conn: AsyncConnection, sql: str, params: dict | None = None) -> str:
    """Run `sql` inside a savepoint and return the SQLSTATE it fails with."""
    nested = await conn.begin_nested()
    with pytest.raises(DBAPIError) as exc_info:
        await conn.execute(text(sql), params or {})
    await nested.rollback()
    return exc_info.value.orig.sqlstate  # type: ignore[union-attr]


# --- layer 1: triggers stop even the owner ---


@pytest.mark.parametrize("table", ["ledger_entries", "transfer_status_history"])
@pytest.mark.parametrize(
    ("statement", "op"),
    [
        ("UPDATE {table} SET created_at = created_at", "UPDATE"),
        ("DELETE FROM {table}", "DELETE"),
        ("TRUNCATE {table}", "TRUNCATE"),
    ],
)
async def test_append_only_tables_reject_changes_even_for_owner(
    owner_tx: AsyncConnection, table: str, statement: str, op: str
) -> None:
    await _seed(owner_tx, with_history=True)

    nested = await owner_tx.begin_nested()
    with pytest.raises(DBAPIError) as exc_info:
        await owner_tx.execute(text(statement.format(table=table)))
    await nested.rollback()

    assert exc_info.value.orig.sqlstate == RAISE_EXCEPTION  # type: ignore[union-attr]
    assert f"{table} is append-only: {op} is not allowed" in str(exc_info.value)


async def test_owner_can_still_append(owner_tx: AsyncConnection) -> None:
    ledger = await _seed(owner_tx, with_history=True)

    count = await owner_tx.scalar(
        text("SELECT count(*) FROM ledger_entries WHERE transfer_id = :x"),
        {"x": ledger.transfer_id},
    )
    assert count == 2


# --- layer 2: wallet_app's grants ---


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entries SET amount = 1",
        "DELETE FROM ledger_entries",
        "TRUNCATE ledger_entries",
        "INSERT INTO transfer_status_history (transfer_id, to_status) VALUES (:x, 'completed')",
        "UPDATE transfer_status_history SET reason = 'forged'",
        "DELETE FROM transfer_status_history",
        "UPDATE transfers SET amount = 1",
        "UPDATE transfers SET to_account_id = from_account_id",
        "DELETE FROM transfers",
        "UPDATE holds SET amount = 1",
        "DELETE FROM holds",
    ],
)
async def test_runtime_role_is_denied(app_tx: AsyncConnection, statement: str) -> None:
    ledger = await _seed(app_tx, with_history=False)
    params = {"x": ledger.transfer_id} if ":x" in statement else None

    assert await _sqlstate(app_tx, statement, params) == INSUFFICIENT_PRIVILEGE


async def test_runtime_role_can_lock_and_update_transfer_status(app_tx: AsyncConnection) -> None:
    ledger = await _seed(app_tx, with_history=False)

    locked = await app_tx.scalar(
        text("SELECT id FROM transfers WHERE id = :x FOR UPDATE"), {"x": ledger.transfer_id}
    )
    await app_tx.execute(
        text("UPDATE transfers SET status = 'reversed', updated_at = now() WHERE id = :x"),
        {"x": ledger.transfer_id},
    )

    assert locked == ledger.transfer_id
    status = await app_tx.scalar(
        text("SELECT status FROM transfers WHERE id = :x"), {"x": ledger.transfer_id}
    )
    assert status == "reversed"


async def test_runtime_role_can_place_and_resolve_a_hold(app_tx: AsyncConnection) -> None:
    ledger = await _seed(app_tx, with_history=False)
    held = await app_tx.scalar(
        text(
            "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, "
            "initiated_by) VALUES ('peer', 'held', 200, :f, :t, :u) RETURNING id"
        ),
        {"f": ledger.sender, "t": ledger.recipient, "u": ledger.user_id},
    )
    hold_id = await app_tx.scalar(
        text(
            "INSERT INTO holds (transfer_id, account_id, amount) VALUES (:x, :f, 200) RETURNING id"
        ),
        {"x": held, "f": ledger.sender},
    )

    await app_tx.execute(
        text(
            "UPDATE holds SET status = 'voided', resolved_at = now(), resolved_by = :u "
            "WHERE id = :h"
        ),
        {"u": ledger.user_id, "h": hold_id},
    )

    status = await app_tx.scalar(text("SELECT status FROM holds WHERE id = :h"), {"h": hold_id})
    assert status == "voided"


async def test_runtime_role_can_read_audit_log(app_tx: AsyncConnection) -> None:
    count = await app_tx.scalar(text("SELECT count(*) FROM transfer_status_history"))
    assert count >= 0
