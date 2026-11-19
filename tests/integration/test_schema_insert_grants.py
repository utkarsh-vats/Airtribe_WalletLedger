"""Migration 0010: wallet_app may insert only the columns a new user or wallet needs.

Runs as wallet_app in rolled-back transactions.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

INSUFFICIENT_PRIVILEGE = "42501"
UNIQUE_VIOLATION = "23505"
CHECK_VIOLATION = "23514"

_USER = (
    "INSERT INTO users (name, email, phone, password_hash{col}) "
    "VALUES ('G', 'g@example.com', '+919800000001', 'x'{val}) RETURNING id"
)


_ACCOUNT = "INSERT INTO accounts (user_id, account_type, label) VALUES "


@pytest.fixture
async def app_tx(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()


async def _user(conn: AsyncConnection) -> int:
    return await conn.scalar(text(_USER.format(col="", val="")))


def _sqlstate_and_constraint(exc: DBAPIError) -> tuple[str, str | None]:
    error = exc.orig.__cause__  # type: ignore[union-attr]
    return error.sqlstate, getattr(error, "constraint_name", None)  # type: ignore[union-attr]


async def test_new_user_takes_role_and_kyc_defaults(app_tx: AsyncConnection) -> None:
    user_id = await _user(app_tx)

    row = (
        await app_tx.execute(
            text("SELECT role, kyc_status FROM users WHERE id = :u"), {"u": user_id}
        )
    ).one()
    assert tuple(row) == ("user", "pending")


async def test_new_wallet_starts_empty(app_tx: AsyncConnection) -> None:
    user_id = await _user(app_tx)

    row = (
        await app_tx.execute(
            text(
                "INSERT INTO accounts (user_id, account_type, label) "
                "VALUES (:u, 'user_wallet', 'main') RETURNING currency, balance, held_amount"
            ),
            {"u": user_id},
        )
    ).one()
    assert tuple(row) == ("INR", 0, 0)


@pytest.mark.parametrize(
    "statement",
    [
        _USER.format(col=", role", val=", 'admin'"),
        _USER.format(col=", kyc_status", val=", 'verified'"),
        _USER.format(col=", role", val=", 'user'"),  # even the default value, if named
        "INSERT INTO accounts (user_id, account_type, label, balance) "
        "VALUES (:u, 'user_wallet', 'rich', 1000000)",
        "INSERT INTO accounts (user_id, account_type, label, held_amount) "
        "VALUES (:u, 'user_wallet', 'held', 0)",
        "INSERT INTO accounts (user_id, account_type, label, currency) "
        "VALUES (:u, 'user_wallet', 'inr', 'INR')",
    ],
    ids=[
        "users.role",
        "users.kyc_status",
        "users.role-default",
        "accounts.balance",
        "accounts.held_amount",
        "accounts.currency",
    ],
)
async def test_restricted_columns_are_denied(app_tx: AsyncConnection, statement: str) -> None:
    params = {"u": await _user(app_tx)} if ":u" in statement else {}

    with pytest.raises(DBAPIError) as exc_info:
        await app_tx.execute(text(statement), params)

    assert _sqlstate_and_constraint(exc_info.value)[0] == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        # A fourth cash_in: one row per system type already exists (0002 seed).
        ("(NULL, 'system_cash_in', 'cash_in_2')", (UNIQUE_VIOLATION, "accounts_system_type_key")),
        ("(NULL, 'system_fee_revenue', 'fees_2')", (UNIQUE_VIOLATION, "accounts_system_type_key")),
        # A system account owned by a user, or a wallet owned by nobody.
        ("(:u, 'system_cash_out', 'mine')", (CHECK_VIOLATION, "accounts_owner_check")),
        ("(NULL, 'user_wallet', 'orphan')", (CHECK_VIOLATION, "accounts_owner_check")),
    ],
    ids=["second-cash-in", "second-fee-revenue", "user-owned-system", "ownerless-wallet"],
)
async def test_runtime_role_cannot_add_a_system_account(
    app_tx: AsyncConnection, values: str, expected: tuple[str, str]
) -> None:
    statement = _ACCOUNT + values
    params = {"u": await _user(app_tx)} if ":u" in statement else {}

    with pytest.raises(DBAPIError) as exc_info:
        await app_tx.execute(text(statement), params)

    assert _sqlstate_and_constraint(exc_info.value) == expected


async def test_insert_privileges_are_column_level_only(owner_tx: AsyncConnection) -> None:
    table_level = await owner_tx.scalar(
        text(
            "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a "
            "WHERE c.relname IN ('users', 'accounts') AND a.grantee = 'wallet_app'::regrole "
            "AND a.privilege_type = 'INSERT'"
        )
    )
    columns = (
        await owner_tx.execute(
            text(
                "SELECT c.relname, att.attname FROM pg_class c "
                "JOIN pg_attribute att ON att.attrelid = c.oid, aclexplode(att.attacl) a "
                "WHERE c.relname IN ('users', 'accounts') AND a.grantee = 'wallet_app'::regrole "
                "AND a.privilege_type = 'INSERT' ORDER BY 1, 2"
            )
        )
    ).all()

    assert table_level == 0
    assert [tuple(r) for r in columns] == [
        ("accounts", "account_type"),
        ("accounts", "label"),
        ("accounts", "user_id"),
        ("users", "email"),
        ("users", "name"),
        ("users", "password_hash"),
        ("users", "phone"),
    ]
