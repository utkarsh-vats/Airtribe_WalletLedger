"""Step 1.6: every CHECK / UNIQUE on the 1.2-1.3 tables rejects its invalid state.

Each case is one statement against a valid baseline (a user, two wallets, a held transfer and
its active hold) and must fail with the expected SQLSTATE *and* constraint name, so a case
can't pass by tripping some other rule. Runs as wallet_owner in a rolled-back transaction:
constraints bind every role, and the owner never hits a privilege error first.

Covered elsewhere: append-only and grants (test_schema_protections), the deferred balance
check (test_schema_balance), the status-history trigger (test_schema_status_history).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

CHECK = "23514"
UNIQUE = "23505"
NOT_NULL = "23502"


@dataclass
class Baseline:
    user_id: int
    a: int  # wallet 'main', balance 1000, held 500
    b: int  # wallet 'savings'
    transfer_id: int  # peer a -> b, held, 500


@pytest.fixture
async def base(owner_tx: AsyncConnection) -> AsyncIterator[Baseline]:
    user_id = await owner_tx.scalar(
        text(
            "INSERT INTO users (name, email, phone, password_hash) "
            "VALUES ('R', 'r@example.test', '+919855555555', 'x') RETURNING id"
        )
    )
    a, b = (
        await owner_tx.execute(
            text(
                "INSERT INTO accounts (user_id, account_type, label, balance, held_amount) "
                "VALUES (:u, 'user_wallet', 'main', 1000, 500), "
                "(:u, 'user_wallet', 'savings', 0, 0) RETURNING id"
            ),
            {"u": user_id},
        )
    ).scalars()
    transfer_id = await owner_tx.scalar(
        text(
            "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, "
            "initiated_by) VALUES ('peer', 'held', 500, :a, :b, :u) RETURNING id"
        ),
        {"a": a, "b": b, "u": user_id},
    )
    await owner_tx.execute(
        text("INSERT INTO holds (transfer_id, account_id, amount) VALUES (:t, :a, 500)"),
        {"t": transfer_id, "a": a},
    )
    yield Baseline(user_id, a, b, transfer_id)


# Placeholders: :u user, :a / :b wallets, :t the held transfer. Not every case uses all.
_USER = "INSERT INTO users (name, email, phone, password_hash{cols}) VALUES ('Z', {vals})"


def _user(email: str = "'z@example.test'", phone: str = "'+919866666666'", **extra: str) -> str:
    cols = "".join(f", {c}" for c in extra)
    vals = ", ".join([email, phone, "'x'", *extra.values()])
    return _USER.format(cols=cols, vals=vals)


_TRANSFER = (
    "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, "
    "parent_transfer_id, reverses_transfer_id, initiated_by) VALUES "
)
_HOLD = "INSERT INTO holds (transfer_id, account_id, amount, status, resolved_at) VALUES "

CASES = [
    # --- users ---
    ("users_role_check", CHECK, _user(role="'root'")),
    ("users_kyc_status_check", CHECK, _user(kyc_status="'maybe'")),
    ("users_phone_e164_check", CHECK, _user(phone="'9876543210'")),  # no +
    ("users_phone_e164_check", CHECK, _user(phone="'+91 98765 43210'")),  # spaces
    ("users_phone_e164_check", CHECK, _user(phone="'+91-9876543210'")),  # dash
    ("users_phone_e164_check", CHECK, _user(phone="'+0919876543210'")),  # leading 0
    ("users_phone_e164_check", CHECK, _user(phone="'+1234567'")),  # 7 digits: too short
    ("users_phone_e164_check", CHECK, _user(phone="'+1234567890123456'")),  # 16: too long
    ("users_phone_key", UNIQUE, _user(phone="'+919855555555'")),
    ("users_email_lower_key", UNIQUE, _user(email="'R@Example.TEST'")),
    # --- accounts ---
    (
        "accounts_account_type_check",
        CHECK,
        "INSERT INTO accounts (user_id, account_type, label) VALUES (:u, 'savings', 'x')",
    ),
    (
        "accounts_currency_check",
        CHECK,
        "INSERT INTO accounts (user_id, account_type, label, currency) "
        "VALUES (:u, 'user_wallet', 'usd', 'USD')",
    ),
    (
        "accounts_owner_check",  # a wallet must have a user
        CHECK,
        "INSERT INTO accounts (user_id, account_type, label) VALUES (NULL, 'user_wallet', 'x')",
    ),
    (
        "accounts_owner_check",  # a system account must not
        CHECK,
        "INSERT INTO accounts (user_id, account_type, label) "
        "VALUES (:u, 'system_fee_revenue', 'x')",
    ),
    ("accounts_held_amount_check", CHECK, "UPDATE accounts SET held_amount = -1 WHERE id = :b"),
    (
        "accounts_available_check",  # balance below held: available 1000 - 1001 < 0
        CHECK,
        "UPDATE accounts SET held_amount = 1001 WHERE id = :a",
    ),
    (
        "accounts_available_check",  # spending reserved funds: 499 - 500 < 0
        CHECK,
        "UPDATE accounts SET balance = 499 WHERE id = :a",
    ),
    ("accounts_available_check", CHECK, "UPDATE accounts SET balance = -1 WHERE id = :b"),
    (
        "accounts_user_label_key",
        UNIQUE,
        "INSERT INTO accounts (user_id, account_type, label) VALUES (:u, 'user_wallet', 'main')",
    ),
    (
        "accounts_system_type_key",
        UNIQUE,
        "INSERT INTO accounts (user_id, account_type, label) "
        "VALUES (NULL, 'system_cash_in', 'cash_in_2')",
    ),
    # --- transfers ---
    ("transfers_type_check", CHECK, _TRANSFER + "('gift', 'held', 1, :a, :b, NULL, NULL, :u)"),
    ("transfers_status_check", CHECK, _TRANSFER + "('peer', 'pending', 1, :a, :b, NULL, NULL, :u)"),
    ("transfers_amount_check", CHECK, _TRANSFER + "('peer', 'held', 0, :a, :b, NULL, NULL, :u)"),
    ("transfers_amount_check", CHECK, _TRANSFER + "('peer', 'held', -5, :a, :b, NULL, NULL, :u)"),
    (
        "transfers_distinct_accounts_check",
        CHECK,
        _TRANSFER + "('peer', 'held', 1, :a, :a, NULL, NULL, :u)",
    ),
    (
        "transfers_reversal_link_check",  # a reversal must name what it reverses
        CHECK,
        _TRANSFER + "('reversal', 'completed', 500, :b, :a, NULL, NULL, :u)",
    ),
    (
        "transfers_reversal_link_check",  # only a reversal may
        CHECK,
        _TRANSFER + "('peer', 'held', 1, :a, :b, NULL, :t, :u)",
    ),
    (
        "transfers_fee_link_check",  # a fee must name its parent
        CHECK,
        _TRANSFER + "('fee', 'completed', 10, :a, :b, NULL, NULL, :u)",
    ),
    (
        "transfers_fee_link_check",  # only a fee may
        CHECK,
        _TRANSFER + "('peer', 'held', 1, :a, :b, :t, NULL, :u)",
    ),
    (
        "transfers_reverses_transfer_id_key",  # a transfer can be reversed once
        UNIQUE,
        _TRANSFER + "('reversal', 'completed', 500, :b, :a, NULL, :t, :u), "
        "('reversal', 'completed', 500, :b, :a, NULL, :t, :u)",
    ),
    (
        "transfers_initiated_by",  # schema.md §4: every transfer has a user (NOT NULL)
        NOT_NULL,
        _TRANSFER + "('peer', 'held', 1, :a, :b, NULL, NULL, NULL)",
    ),
    # --- ledger_entries ---
    (
        "ledger_entries_amount_check",
        CHECK,
        "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
        "VALUES (:t, :a, 'debit', 0)",
    ),
    (
        "ledger_entries_amount_check",  # the sign lives in direction, never in amount
        CHECK,
        "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
        "VALUES (:t, :a, 'credit', -500)",
    ),
    (
        "ledger_entries_direction_check",
        CHECK,
        "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
        "VALUES (:t, :a, 'up', 500)",
    ),
    # --- holds (the baseline already has the active hold for :t) ---
    ("holds_transfer_id_key", UNIQUE, _HOLD + "(:t, :a, 500, 'active', NULL)"),
    ("holds_amount_check", CHECK, "UPDATE holds SET amount = 0 WHERE transfer_id = :t"),
    (
        "holds_status_check",  # resolved_at set so holds_resolution_check can't fire first
        CHECK,
        "UPDATE holds SET status = 'pending', resolved_at = now() WHERE transfer_id = :t",
    ),
    (
        "holds_resolution_check",  # resolved without a time
        CHECK,
        "UPDATE holds SET status = 'voided' WHERE transfer_id = :t",
    ),
    (
        "holds_resolution_check",  # active with a time
        CHECK,
        "UPDATE holds SET resolved_at = now() WHERE transfer_id = :t",
    ),
    # --- transfer_status_history (only the trigger writes it; the owner can, to test) ---
    (
        "transfer_status_history_to_status_check",
        CHECK,
        "INSERT INTO transfer_status_history (transfer_id, to_status) VALUES (:t, 'paid')",
    ),
    (
        "transfer_status_history_from_status_check",
        CHECK,
        "INSERT INTO transfer_status_history (transfer_id, from_status, to_status) "
        "VALUES (:t, 'pending', 'held')",
    ),
]


@pytest.mark.parametrize(("constraint", "sqlstate", "statement"), CASES, ids=[c[0] for c in CASES])
async def test_invalid_state_is_rejected(
    owner_tx: AsyncConnection, base: Baseline, constraint: str, sqlstate: str, statement: str
) -> None:
    params = {"u": base.user_id, "a": base.a, "b": base.b, "t": base.transfer_id}
    used = {k: v for k, v in params.items() if f":{k}" in statement}

    with pytest.raises(DBAPIError) as exc_info:
        await owner_tx.execute(text(statement), used)

    error = exc_info.value.orig.__cause__  # the asyncpg exception carries the details
    assert error.sqlstate == sqlstate, str(error)  # type: ignore[union-attr]
    if sqlstate == NOT_NULL:
        table, column = constraint.split("_", 1)
        assert (error.table_name, error.column_name) == (table, column)  # type: ignore[union-attr]
    else:
        assert error.constraint_name == constraint, str(error)  # type: ignore[union-attr]


def test_every_constraint_on_these_tables_has_a_case() -> None:
    """Adding a CHECK or UNIQUE without a rejecting case here fails this test (see below)."""
    assert {c for c, s, _ in CASES if s != NOT_NULL} == COVERED


COVERED = {
    "users_role_check",
    "users_kyc_status_check",
    "users_phone_e164_check",
    "users_phone_key",
    "users_email_lower_key",
    "accounts_account_type_check",
    "accounts_currency_check",
    "accounts_owner_check",
    "accounts_held_amount_check",
    "accounts_available_check",
    "accounts_user_label_key",
    "accounts_system_type_key",
    "transfers_type_check",
    "transfers_status_check",
    "transfers_amount_check",
    "transfers_distinct_accounts_check",
    "transfers_reversal_link_check",
    "transfers_fee_link_check",
    "transfers_reverses_transfer_id_key",
    "ledger_entries_amount_check",
    "ledger_entries_direction_check",
    "holds_transfer_id_key",
    "holds_amount_check",
    "holds_status_check",
    "holds_resolution_check",
    "transfer_status_history_to_status_check",
    "transfer_status_history_from_status_check",
}


async def test_catalog_has_no_unlisted_constraints(owner_tx: AsyncConnection) -> None:
    """Every CHECK, UNIQUE constraint and unique index on these tables has a case above."""
    rows = await owner_tx.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = ANY(CAST(:t AS regclass[])) AND contype IN ('c', 'u') "
            "UNION "
            "SELECT i.indexrelid::regclass::text FROM pg_index i "
            "WHERE i.indrelid = ANY(CAST(:t AS regclass[])) AND i.indisunique "
            "AND NOT i.indisprimary "
            "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)"
        ),
        {
            "t": [
                "users",
                "accounts",
                "transfers",
                "ledger_entries",
                "holds",
                "transfer_status_history",
            ]
        },
    )
    assert set(rows.scalars()) == COVERED
