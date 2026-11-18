"""Migration 0002: seeded system accounts and wallet_app's grants.

Each test runs in a transaction that is rolled back, so the test database keeps only the
migrated baseline. The full "every protection rejects its invalid state" suite is step 1.6.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

INSUFFICIENT_PRIVILEGE = "42501"


@pytest.fixture
async def tx(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """wallet_app connection inside a transaction that is always rolled back."""
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()


async def _new_user(conn: AsyncConnection, n: int = 1) -> int:
    return await conn.scalar(
        text(
            "INSERT INTO users (name, email, phone, password_hash) "
            "VALUES (:name, :email, :phone, 'x') RETURNING id"
        ),
        {"name": f"User {n}", "email": f"user{n}@example.test", "phone": f"+9198765432{n:02d}"},
    )


async def test_system_accounts_seeded_with_lowest_ids(tx: AsyncConnection) -> None:
    rows = (
        await tx.execute(
            text(
                "SELECT id, account_type, label, balance, held_amount "
                "FROM accounts WHERE user_id IS NULL ORDER BY id"
            )
        )
    ).all()

    assert [tuple(r) for r in rows] == [
        (1, "system_cash_in", "cash_in", 0, 0),
        (2, "system_cash_out", "cash_out", 0, 0),
        (3, "system_fee_revenue", "fee_revenue", 0, 0),
    ]


async def test_runtime_role_can_create_user_and_wallet(tx: AsyncConnection) -> None:
    user_id = await _new_user(tx)
    wallet = (
        await tx.execute(
            text(
                "INSERT INTO accounts (user_id, account_type, label) "
                "VALUES (:u, 'user_wallet', 'main') "
                "RETURNING id, currency, balance, held_amount"
            ),
            {"u": user_id},
        )
    ).one()

    assert wallet.id > 3  # system accounts hold the lowest ids
    assert (wallet.currency, wallet.balance, wallet.held_amount) == ("INR", 0, 0)
    role, kyc = (
        await tx.execute(text("SELECT role, kyc_status FROM users WHERE id = :u"), {"u": user_id})
    ).one()
    assert (role, kyc) == ("user", "pending")


async def test_runtime_role_can_update_only_granted_columns(tx: AsyncConnection) -> None:
    user_id = await _new_user(tx)
    await tx.execute(
        text("UPDATE users SET kyc_status = 'verified', updated_at = now() WHERE id = :u"),
        {"u": user_id},
    )

    with pytest.raises(DBAPIError) as exc_info:
        await tx.execute(text("UPDATE users SET role = 'admin' WHERE id = :u"), {"u": user_id})
    assert exc_info.value.orig.sqlstate == INSUFFICIENT_PRIVILEGE  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM users",
        "DELETE FROM accounts",
        "UPDATE accounts SET user_id = NULL",
        "UPDATE accounts SET account_type = 'system_cash_in'",
        "TRUNCATE accounts",
    ],
)
async def test_runtime_role_is_denied(tx: AsyncConnection, statement: str) -> None:
    with pytest.raises(DBAPIError) as exc_info:
        await tx.execute(text(statement))

    assert exc_info.value.orig.sqlstate == INSUFFICIENT_PRIVILEGE  # type: ignore[union-attr]
