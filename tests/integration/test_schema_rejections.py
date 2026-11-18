"""Step 1.6: every CHECK, UNIQUE and unique index in the schema rejects its invalid state.

Each case is one statement against a valid baseline (a user, two wallets, a held transfer, its
active hold, and a queued reconciliation run) and must fail with the expected SQLSTATE *and*
constraint name, so a case can't pass by tripping some other rule. Runs as wallet_owner in a
rolled-back transaction: constraints bind every role, and the owner never hits a privilege
error first.

Covered elsewhere: append-only and grants (test_schema_protections), the deferred balance
check (test_schema_balance), the status-history trigger (test_schema_status_history).

A catalog guard fails if any CHECK / UNIQUE / unique index in the public schema has no case.
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
    run_id: int  # reconciliation run, queued


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
    run_id = await owner_tx.scalar(
        text("INSERT INTO reconciliation_runs (trigger) VALUES ('manual') RETURNING id")
    )
    yield Baseline(user_id, a, b, transfer_id, run_id)


# Placeholders: :u user, :a / :b wallets, :t the held transfer, :r the run. NOT NULL cases are
# named "table.column" (Postgres reports no constraint name for them).
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
_KEY = (
    "INSERT INTO idempotency_keys (user_id, key, request_hash, status, response_code, "
    "response_body, completed_at) VALUES "
)
_HEX = "repeat('a', 64)"
_BODY = "CAST('{}' AS JSONB)"
_VELOCITY = "(SELECT id FROM risk_rules WHERE code = 'velocity')"
_FLAG = (
    "INSERT INTO risk_flags (transfer_id, rule_id, action_taken, details, llm_explanation, "
    "llm_label, llm_model, explained_at) VALUES "
)
_RUN = (
    "INSERT INTO reconciliation_runs (trigger, status, started_at, finished_at, checks_run, "
    "issue_count, error_message) VALUES "
)
_ISSUE = "INSERT INTO reconciliation_issues (run_id, check_name, entity_type, entity_id) VALUES "

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
        "transfers.initiated_by",  # schema.md §4: every transfer has a user (NOT NULL)
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
    # --- idempotency_keys ---
    (
        "idempotency_keys_user_key_key",  # NULLS NOT DISTINCT: two unauthenticated rows clash
        UNIQUE,
        _KEY + f"(NULL, '00000000-0000-0000-0000-000000000001', {_HEX}, DEFAULT, NULL, NULL, "
        f"NULL), (NULL, '00000000-0000-0000-0000-000000000001', {_HEX}, DEFAULT, NULL, NULL, NULL)",
    ),
    (
        "idempotency_keys_status_check",
        CHECK,
        _KEY + f"(:u, gen_random_uuid(), {_HEX}, 'done', NULL, NULL, NULL)",
    ),
    (
        "idempotency_keys_request_hash_check",  # uppercase hex
        CHECK,
        _KEY + "(:u, gen_random_uuid(), repeat('A', 64), DEFAULT, NULL, NULL, NULL)",
    ),
    (
        "idempotency_keys_request_hash_check",  # too short (CHAR pads with spaces)
        CHECK,
        _KEY + "(:u, gen_random_uuid(), 'abc', DEFAULT, NULL, NULL, NULL)",
    ),
    (
        "idempotency_keys_response_code_check",  # a 5xx is never stored
        CHECK,
        _KEY + f"(:u, gen_random_uuid(), {_HEX}, 'completed', 500, {_BODY}, now())",
    ),
    (
        "idempotency_keys_response_code_check",
        CHECK,
        _KEY + f"(:u, gen_random_uuid(), {_HEX}, 'completed', 199, {_BODY}, now())",
    ),
    (
        "idempotency_keys_completion_check",  # completed without its body
        CHECK,
        _KEY + f"(:u, gen_random_uuid(), {_HEX}, 'completed', 201, NULL, now())",
    ),
    (
        "idempotency_keys_completion_check",  # completed without completed_at
        CHECK,
        _KEY + f"(:u, gen_random_uuid(), {_HEX}, 'completed', 201, {_BODY}, NULL)",
    ),
    (
        "idempotency_keys_completion_check",  # in progress but already has a response
        CHECK,
        _KEY + f"(:u, gen_random_uuid(), {_HEX}, 'in_progress', 201, NULL, NULL)",
    ),
    # --- risk_rules ---
    (
        "risk_rules_code_check",
        CHECK,
        "INSERT INTO risk_rules (code, action, params) "
        "VALUES ('night_owl', 'flag', CAST('{}' AS JSONB))",
    ),
    (
        "risk_rules_code_key",
        UNIQUE,
        "INSERT INTO risk_rules (code, action, params) "
        "VALUES ('velocity', 'flag', CAST('{}' AS JSONB))",
    ),
    ("risk_rules_action_check", CHECK, "UPDATE risk_rules SET action = 'block'"),
    (
        "risk_rules_params_check",
        CHECK,
        "UPDATE risk_rules SET params = CAST('[10, 5]' AS JSONB) WHERE code = 'velocity'",
    ),
    # --- risk_flags ---
    (
        "risk_flags_transfer_rule_key",  # one flag per rule per transfer
        UNIQUE,
        _FLAG + f"(:t, {_VELOCITY}, 'hold', {_BODY}, NULL, NULL, NULL, NULL), "
        f"(:t, {_VELOCITY}, 'hold', {_BODY}, NULL, NULL, NULL, NULL)",
    ),
    (
        "risk_flags_action_taken_check",
        CHECK,
        _FLAG + f"(:t, {_VELOCITY}, 'block', {_BODY}, NULL, NULL, NULL, NULL)",
    ),
    (
        "risk_flags_details_check",
        CHECK,
        _FLAG + f"(:t, {_VELOCITY}, 'hold', CAST('[1]' AS JSONB), NULL, NULL, NULL, NULL)",
    ),
    (
        "risk_flags.details",  # schema.md §4: every flag records what the rule observed
        NOT_NULL,
        _FLAG + f"(:t, {_VELOCITY}, 'hold', NULL, NULL, NULL, NULL, NULL)",
    ),
    (
        "risk_flags_llm_label_check",  # complete explanation, unknown label
        CHECK,
        _FLAG + f"(:t, {_VELOCITY}, 'hold', {_BODY}, 'two sentences', 'fraud', 'm', now())",
    ),
    (
        "risk_flags_explanation_check",  # label without the rest
        CHECK,
        _FLAG + f"(:t, {_VELOCITY}, 'hold', {_BODY}, NULL, 'review', NULL, NULL)",
    ),
    (
        "risk_flags_explanation_check",  # everything but the timestamp
        CHECK,
        _FLAG + f"(:t, {_VELOCITY}, 'hold', {_BODY}, 'two sentences', 'review', 'm', NULL)",
    ),
    # --- reconciliation_runs ---
    (
        "reconciliation_runs_trigger_check",
        CHECK,
        _RUN + "('cron', 'queued', NULL, NULL, 0, 0, NULL)",
    ),
    (
        "reconciliation_runs_status_check",  # timestamps set so the lifecycle check passes
        CHECK,
        _RUN + "('manual', 'paused', now(), now(), 0, 0, NULL)",
    ),
    (
        "reconciliation_runs_counts_check",
        CHECK,
        _RUN + "('manual', 'queued', NULL, NULL, -1, 0, NULL)",
    ),
    (
        "reconciliation_runs_counts_check",
        CHECK,
        _RUN + "('manual', 'queued', NULL, NULL, 0, -1, NULL)",
    ),
    (
        "reconciliation_runs_lifecycle_check",  # queued but started
        CHECK,
        _RUN + "('manual', 'queued', now(), NULL, 0, 0, NULL)",
    ),
    (
        "reconciliation_runs_lifecycle_check",  # running but never started
        CHECK,
        _RUN + "('manual', 'running', NULL, NULL, 0, 0, NULL)",
    ),
    (
        "reconciliation_runs_lifecycle_check",  # running but already finished
        CHECK,
        _RUN + "('manual', 'running', now(), now(), 0, 0, NULL)",
    ),
    (
        "reconciliation_runs_lifecycle_check",  # finished without finished_at
        CHECK,
        _RUN + "('manual', 'passed', now(), NULL, 6, 0, NULL)",
    ),
    (
        "reconciliation_runs_lifecycle_check",  # finished before it started
        CHECK,
        _RUN + "('manual', 'passed', now(), now() - interval '1 second', 6, 0, NULL)",
    ),
    (
        "reconciliation_runs_outcome_check",  # passed with issues
        CHECK,
        _RUN + "('manual', 'passed', now(), now(), 6, 2, NULL)",
    ),
    (
        "reconciliation_runs_outcome_check",  # failed without issues
        CHECK,
        _RUN + "('manual', 'failed', now(), now(), 6, 0, NULL)",
    ),
    (
        "reconciliation_runs_outcome_check",  # error without a message
        CHECK,
        _RUN + "('manual', 'error', now(), now(), 0, 0, NULL)",
    ),
    # --- reconciliation_issues ---
    ("reconciliation_issues_entity_type_check", CHECK, _ISSUE + "(:r, 'c', 'wallet', 1)"),
    (
        "reconciliation_issues_entity_check",  # a ledger-wide issue has no entity
        CHECK,
        _ISSUE + "(:r, 'global_balance', 'ledger', 1)",
    ),
    (
        "reconciliation_issues_entity_check",  # every other issue has one
        CHECK,
        _ISSUE + "(:r, 'orphan_entry', 'entry', NULL)",
    ),
    # --- job_locks ---
    (
        "job_locks_job_name_key",
        UNIQUE,
        "INSERT INTO job_locks (job_name) VALUES ('reconciliation')",
    ),
]


@pytest.mark.parametrize(("constraint", "sqlstate", "statement"), CASES, ids=[c[0] for c in CASES])
async def test_invalid_state_is_rejected(
    owner_tx: AsyncConnection, base: Baseline, constraint: str, sqlstate: str, statement: str
) -> None:
    params = {
        "u": base.user_id,
        "a": base.a,
        "b": base.b,
        "t": base.transfer_id,
        "r": base.run_id,
    }
    used = {k: v for k, v in params.items() if f":{k}" in statement}

    with pytest.raises(DBAPIError) as exc_info:
        await owner_tx.execute(text(statement), used)

    error = exc_info.value.orig.__cause__  # the asyncpg exception carries the details
    assert error.sqlstate == sqlstate, str(error)  # type: ignore[union-attr]
    if sqlstate == NOT_NULL:
        table, column = constraint.split(".")
        assert (error.table_name, error.column_name) == (table, column)  # type: ignore[union-attr]
    else:
        assert error.constraint_name == constraint, str(error)  # type: ignore[union-attr]


async def test_every_constraint_in_the_schema_has_a_case(owner_tx: AsyncConnection) -> None:
    """Adding a CHECK, UNIQUE constraint or unique index anywhere in the public schema without
    a rejecting case above fails here. Primary keys are left out (identity columns)."""
    rows = await owner_tx.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE connamespace = 'public'::regnamespace AND contype IN ('c', 'u') "
            "UNION "
            "SELECT ic.relname FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE ic.relnamespace = 'public'::regnamespace AND i.indisunique "
            "AND NOT i.indisprimary "
            "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)"
        )
    )
    covered = {name for name, sqlstate, _ in CASES if sqlstate != NOT_NULL}

    catalog = set(rows.scalars())
    assert catalog - covered == set(), "constraints without a rejecting case"
    assert covered - catalog == set(), "cases naming constraints that don't exist"
