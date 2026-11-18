"""Migration 0007: idempotency_keys, risk_rules, risk_flags, reconciliation runs/issues, job_locks.

Structure, seeds, grants, and the two behaviours later steps lean on: NULLS NOT DISTINCT key
scoping (§5) and the job-lock NOWAIT conflict (§8.1). Every row is rolled back. The full
"every protection rejects its invalid state" suite is step 1.6.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

UNIQUE_VIOLATION = "23505"
LOCK_NOT_AVAILABLE = "55P03"
INSUFFICIENT_PRIVILEGE = "42501"

TABLES = (
    "idempotency_keys",
    "risk_rules",
    "risk_flags",
    "reconciliation_runs",
    "reconciliation_issues",
    "job_locks",
)

# DESIGN.md §3.4 for these tables.
EXPECTED_INDEXES = {
    "idempotency_keys_created_idx": "(created_at)",
    "risk_flags_unexplained_idx": "(created_at) WHERE (llm_label IS NULL)",
    "reconciliation_issues_run_idx": "(run_id)",
}

CONSTRAINT_INDEXES = {
    "idempotency_keys_pkey": "(id)",
    "idempotency_keys_user_key_key": "(user_id, key) NULLS NOT DISTINCT",
    "risk_rules_pkey": "(id)",
    "risk_rules_code_key": "(code)",
    "risk_flags_pkey": "(id)",
    "risk_flags_transfer_rule_key": "(transfer_id, rule_id)",
    "reconciliation_runs_pkey": "(id)",
    "reconciliation_issues_pkey": "(id)",
    "job_locks_pkey": "(id)",
    "job_locks_job_name_key": "(job_name)",
}

# wallet_app privileges: table-level, then column-level UPDATE.
EXPECTED_TABLE_PRIVILEGES = {
    "idempotency_keys": {"SELECT", "INSERT", "DELETE"},
    "risk_rules": {"SELECT"},
    "risk_flags": {"SELECT", "INSERT"},
    "reconciliation_runs": {"SELECT", "INSERT"},
    "reconciliation_issues": {"SELECT", "INSERT"},
    "job_locks": {"SELECT"},
}
EXPECTED_COLUMN_UPDATES = {
    "idempotency_keys": {"status", "response_code", "response_body", "completed_at"},
    "risk_flags": {"llm_explanation", "llm_label", "llm_model", "explained_at"},
    "reconciliation_runs": {
        "status",
        "started_at",
        "finished_at",
        "checks_run",
        "issue_count",
        "error_message",
    },
    "job_locks": {"last_started_at"},
}


@pytest.fixture
async def app_tx(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()


def _sqlstate(exc: DBAPIError) -> str:
    return exc.orig.sqlstate  # type: ignore[union-attr]


# --- structure ---


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

    assert defs == EXPECTED_INDEXES | CONSTRAINT_INDEXES


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


# --- seeds ---


async def test_risk_rules_seeded_from_design(app_tx: AsyncConnection) -> None:
    rows = (
        await app_tx.execute(text("SELECT code, action, params, enabled FROM risk_rules"))
    ).all()

    assert {r.code: (r.action, r.params, r.enabled) for r in rows} == {
        "velocity": ("hold", {"window_minutes": 10, "max_count": 5}, True),
        "amount_spike": ("hold", {"lookback_days": 30, "multiplier": 5}, True),
        "new_payee_large": ("flag", {"threshold_paise": 1000000}, True),
    }


async def test_job_locks_seeded_one_row_per_job(app_tx: AsyncConnection) -> None:
    rows = (await app_tx.execute(text("SELECT job_name, last_started_at FROM job_locks"))).all()

    assert sorted(rows) == [
        ("flag_explanations", None),
        ("idempotency_cleanup", None),
        ("reconciliation", None),
    ]


# --- grants ---


async def test_runtime_role_privileges_are_exactly_least_privilege(
    owner_tx: AsyncConnection,
) -> None:
    table_rows = (
        await owner_tx.execute(
            text(
                "SELECT c.relname, a.privilege_type "
                "FROM pg_class c, aclexplode(c.relacl) a "
                "WHERE c.relname = ANY(:t) AND a.grantee = 'wallet_app'::regrole"
            ),
            {"t": list(TABLES)},
        )
    ).all()
    column_rows = (
        await owner_tx.execute(
            text(
                "SELECT c.relname, att.attname, a.privilege_type "
                "FROM pg_class c JOIN pg_attribute att ON att.attrelid = c.oid, "
                "aclexplode(att.attacl) a "
                "WHERE c.relname = ANY(:t) AND a.grantee = 'wallet_app'::regrole"
            ),
            {"t": list(TABLES)},
        )
    ).all()

    table_privileges: dict[str, set[str]] = {}
    for table, privilege in table_rows:
        table_privileges.setdefault(table, set()).add(privilege)
    column_updates: dict[str, set[str]] = {}
    for table, column, privilege in column_rows:
        assert privilege == "UPDATE", (table, column, privilege)
        column_updates.setdefault(table, set()).add(column)

    assert table_privileges == EXPECTED_TABLE_PRIVILEGES
    assert column_updates == EXPECTED_COLUMN_UPDATES


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE risk_rules SET enabled = false",
        "INSERT INTO job_locks (job_name) VALUES ('rogue')",
        "DELETE FROM reconciliation_issues",
        "UPDATE idempotency_keys SET request_hash = repeat('0', 64)",
    ],
)
async def test_runtime_role_is_denied(app_tx: AsyncConnection, statement: str) -> None:
    with pytest.raises(DBAPIError) as exc_info:
        await app_tx.execute(text(statement))
    assert _sqlstate(exc_info.value) == INSUFFICIENT_PRIVILEGE


# --- idempotency key scoping (§5) ---


async def _insert_key(conn: AsyncConnection, user_id: int | None, key: uuid.UUID) -> int | None:
    """The §5 'seen before' probe: returns the new id, or None if the key already exists."""
    return await conn.scalar(
        text(
            "INSERT INTO idempotency_keys (user_id, key, request_hash) "
            "VALUES (:u, :k, repeat('a', 64)) "
            "ON CONFLICT (user_id, key) DO NOTHING RETURNING id"
        ),
        {"u": user_id, "k": key},
    )


async def test_unauthenticated_keys_conflict_despite_null_user(app_tx: AsyncConnection) -> None:
    """NULLS NOT DISTINCT: two POST /users retries with one key are the same request."""
    key = uuid.uuid4()

    assert await _insert_key(app_tx, None, key) is not None
    assert await _insert_key(app_tx, None, key) is None


async def test_same_key_is_independent_per_user(app_tx: AsyncConnection) -> None:
    users = (
        await app_tx.execute(
            text(
                "INSERT INTO users (name, email, phone, password_hash) VALUES "
                "('I1', 'i1@example.test', '+919844444441', 'x'), "
                "('I2', 'i2@example.test', '+919844444442', 'x') RETURNING id"
            )
        )
    ).scalars()
    u1, u2 = users
    key = uuid.uuid4()

    assert await _insert_key(app_tx, u1, key) is not None
    assert await _insert_key(app_tx, u2, key) is not None
    assert await _insert_key(app_tx, None, key) is not None
    with pytest.raises(DBAPIError) as exc_info:
        await app_tx.execute(
            text(
                "INSERT INTO idempotency_keys (user_id, key, request_hash) "
                "VALUES (:u, :k, repeat('b', 64))"
            ),
            {"u": u1, "k": key},
        )
    assert _sqlstate(exc_info.value) == UNIQUE_VIOLATION


async def test_runtime_role_can_complete_and_clean_up_a_key(app_tx: AsyncConnection) -> None:
    key_id = await _insert_key(app_tx, None, uuid.uuid4())

    await app_tx.execute(
        text(
            "UPDATE idempotency_keys SET status = 'completed', response_code = 201, "
            "response_body = CAST(:b AS JSONB), completed_at = now() WHERE id = :i"
        ),
        {"b": '{"id": 1}', "i": key_id},
    )
    deleted = await app_tx.execute(
        text("DELETE FROM idempotency_keys WHERE id = :i AND status = 'completed'"),
        {"i": key_id},
    )
    assert deleted.rowcount == 1


# --- job lock (§8.1) ---


async def test_job_lock_second_holder_fails_fast(
    app_tx: AsyncConnection, engine: AsyncEngine
) -> None:
    """wallet_app can take the reconciliation lock (needs the UPDATE (last_started_at) grant),
    and a second run trying the same lock gets lock_not_available immediately."""
    lock_sql = text("SELECT id FROM job_locks WHERE job_name = 'reconciliation' FOR UPDATE NOWAIT")
    assert await app_tx.scalar(lock_sql) is not None
    await app_tx.execute(
        text("UPDATE job_locks SET last_started_at = now() WHERE job_name = 'reconciliation'")
    )

    async with engine.connect() as other:
        await other.begin()
        with pytest.raises(DBAPIError) as exc_info:
            await other.execute(lock_sql)
        assert _sqlstate(exc_info.value) == LOCK_NOT_AVAILABLE
        await other.rollback()

        # Other jobs' rows are independent locks.
        async with other.begin():
            other_job = await other.scalar(
                text(
                    "SELECT id FROM job_locks WHERE job_name = 'idempotency_cleanup' "
                    "FOR UPDATE NOWAIT"
                )
            )
        assert other_job is not None
