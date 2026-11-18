"""Migration 0003: transfers, ledger_entries, holds, transfer_status_history.

Runs as wallet_owner in rolled-back transactions: wallet_app has no grants on these tables
until 0004. The full "every protection rejects its invalid state" suite is step 1.6.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# DESIGN.md §3.4, for the tables created in 0003: index name -> definition after "USING btree".
EXPECTED_INDEXES = {
    "ledger_entries_account_created_idx": "(account_id, created_at DESC, id DESC)",
    "ledger_entries_transfer_idx": "(transfer_id)",
    "transfers_from_created_idx": "(from_account_id, created_at) INCLUDE (amount, status, type)",
    "transfers_completed_pair_idx": "(from_account_id, to_account_id) "
    "WHERE (status = 'completed'::text)",
    "transfers_to_created_idx": "(to_account_id, created_at)",
    "transfers_held_created_idx": "(created_at) WHERE (status = 'held'::text)",
    "transfers_parent_transfer_idx": "(parent_transfer_id) WHERE (parent_transfer_id IS NOT NULL)",
    "transfer_status_history_transfer_idx": "(transfer_id)",
}

# Indexes Postgres creates for primary keys and UNIQUE constraints.
CONSTRAINT_INDEXES = {
    "transfers_pkey",
    "transfers_reverses_transfer_id_key",
    "ledger_entries_pkey",
    "holds_pkey",
    "holds_transfer_id_key",
    "transfer_status_history_pkey",
}

TABLES = ("transfers", "ledger_entries", "holds", "transfer_status_history")


async def test_indexes_match_design(owner_tx: AsyncConnection) -> None:
    rows = (
        await owner_tx.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = ANY(:tables)"
            ),
            {"tables": list(TABLES)},
        )
    ).all()
    defs = {name: d.split(" USING btree ", 1)[1] for name, d in rows}

    assert set(defs) == set(EXPECTED_INDEXES) | CONSTRAINT_INDEXES
    for name, expected in EXPECTED_INDEXES.items():
        assert defs[name] == expected, name


async def test_tables_owned_by_schema_owner(owner_tx: AsyncConnection) -> None:
    owners = dict(
        (
            await owner_tx.execute(
                text("SELECT tablename, tableowner FROM pg_tables WHERE tablename = ANY(:t)"),
                {"t": list(TABLES)},
            )
        ).all()
    )

    assert owners == dict.fromkeys(TABLES, "wallet_owner")


async def test_settled_peer_transfer_and_hold_fit_the_schema(owner_tx: AsyncConnection) -> None:
    """Smoke test that the FKs and defaults line up for the shapes the services will write."""
    q = owner_tx.execute
    user_id = await owner_tx.scalar(
        text(
            "INSERT INTO users (name, email, phone, password_hash) "
            "VALUES ('A', 'a@example.test', '+919800000001', 'x') RETURNING id"
        )
    )
    sender, recipient = (
        await q(
            text(
                "INSERT INTO accounts (user_id, account_type, label, balance) "
                "VALUES (:u, 'user_wallet', 'main', 1000), (:u, 'user_wallet', 'savings', 0) "
                "RETURNING id"
            ),
            {"u": user_id},
        )
    ).scalars()

    settled = await owner_tx.scalar(
        text(
            "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, "
            "initiated_by) VALUES ('peer', 'completed', 500, :f, :t, :u) RETURNING id"
        ),
        {"f": sender, "t": recipient, "u": user_id},
    )
    await q(
        text(
            "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
            "VALUES (:x, :f, 'debit', 500), (:x, :t, 'credit', 500)"
        ),
        {"x": settled, "f": sender, "t": recipient},
    )
    await q(
        text(
            "INSERT INTO transfer_status_history (transfer_id, from_status, to_status, changed_by) "
            "VALUES (:x, NULL, 'completed', :u)"
        ),
        {"x": settled, "u": user_id},
    )

    held = await owner_tx.scalar(
        text(
            "INSERT INTO transfers (type, status, amount, from_account_id, to_account_id, "
            "initiated_by) VALUES ('peer', 'held', 200, :f, :t, :u) RETURNING id"
        ),
        {"f": sender, "t": recipient, "u": user_id},
    )
    hold_status = await owner_tx.scalar(
        text(
            "INSERT INTO holds (transfer_id, account_id, amount) "
            "VALUES (:x, :f, 200) RETURNING status"
        ),
        {"x": held, "f": sender},
    )

    entries = await owner_tx.scalar(
        text("SELECT count(*) FROM ledger_entries WHERE transfer_id = ANY(:ids)"),
        {"ids": [settled, held]},
    )
    assert hold_status == "active"
    assert entries == 2  # the held transfer has no entries until it settles
