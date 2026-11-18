"""transfers, ledger_entries, holds, transfer_status_history and their indexes

Tables from DESIGN.md §3.2 (plus docs/schema.md §4: initiated_by NOT NULL, holds.amount > 0),
indexes from §3.4. No grants here: wallet_app gets its privileges on these tables in 0004,
together with the immutability and audit triggers.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    # --- transfers: the business event ---
    """
    CREATE TABLE transfers (
        id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        type                 TEXT        NOT NULL,
        status               TEXT        NOT NULL,
        amount               BIGINT      NOT NULL,   -- paise
        from_account_id      BIGINT      NOT NULL REFERENCES accounts (id),
        to_account_id        BIGINT      NOT NULL REFERENCES accounts (id),
        parent_transfer_id   BIGINT      REFERENCES transfers (id),   -- fee -> its parent
        reverses_transfer_id BIGINT      REFERENCES transfers (id),   -- reversal -> original
        note                 TEXT,                                    -- user input
        initiated_by         BIGINT      NOT NULL REFERENCES users (id),
        created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT transfers_type_check CHECK (
            type IN ('topup', 'peer', 'withdrawal', 'fee', 'reversal')
        ),
        CONSTRAINT transfers_status_check CHECK (
            status IN ('held', 'completed', 'rejected', 'reversed')
        ),
        CONSTRAINT transfers_amount_check            CHECK (amount > 0),
        CONSTRAINT transfers_distinct_accounts_check CHECK (from_account_id <> to_account_id),
        CONSTRAINT transfers_reversal_link_check CHECK (
            (type = 'reversal') = (reverses_transfer_id IS NOT NULL)
        ),
        CONSTRAINT transfers_fee_link_check CHECK (
            (type = 'fee') = (parent_transfer_id IS NOT NULL)
        ),
        -- A transfer can be reversed once: a second reversal is a constraint violation.
        CONSTRAINT transfers_reverses_transfer_id_key UNIQUE (reverses_transfer_id)
    )
    """,
    # --- ledger_entries: append-only debit/credit lines, source of truth for balances ---
    """
    CREATE TABLE ledger_entries (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        transfer_id BIGINT      NOT NULL REFERENCES transfers (id),
        account_id  BIGINT      NOT NULL REFERENCES accounts (id),
        direction   TEXT        NOT NULL,
        amount      BIGINT      NOT NULL,   -- paise; sign lives in direction, never in amount
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT ledger_entries_direction_check CHECK (direction IN ('debit', 'credit')),
        CONSTRAINT ledger_entries_amount_check    CHECK (amount > 0)
    )
    """,
    # --- holds: funds reserved for a held transfer ---
    """
    CREATE TABLE holds (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        transfer_id BIGINT      NOT NULL REFERENCES transfers (id),
        account_id  BIGINT      NOT NULL REFERENCES accounts (id),
        amount      BIGINT      NOT NULL,   -- paise
        status      TEXT        NOT NULL DEFAULT 'active',
        resolved_at TIMESTAMPTZ,
        resolved_by BIGINT      REFERENCES users (id),
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT holds_transfer_id_key UNIQUE (transfer_id),
        CONSTRAINT holds_amount_check    CHECK (amount > 0),
        CONSTRAINT holds_status_check    CHECK (status IN ('active', 'captured', 'voided'))
    )
    """,
    # --- transfer_status_history: audit log, written by the 0004 trigger ---
    """
    CREATE TABLE transfer_status_history (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        transfer_id BIGINT      NOT NULL REFERENCES transfers (id),
        from_status TEXT,                                  -- NULL for the insert
        to_status   TEXT        NOT NULL,
        changed_by  BIGINT      REFERENCES users (id),     -- NULL = system
        reason      TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT transfer_status_history_from_status_check CHECK (
            from_status IN ('held', 'completed', 'rejected', 'reversed')
        ),
        CONSTRAINT transfer_status_history_to_status_check CHECK (
            to_status IN ('held', 'completed', 'rejected', 'reversed')
        )
    )
    """,
    # --- indexes, exactly DESIGN.md §3.4 (each one costs a write on the transfer path) ---
    # Statement, offset and cursor pagination, Q&A.
    """
    CREATE INDEX ledger_entries_account_created_idx
        ON ledger_entries (account_id, created_at DESC, id DESC)
    """,
    # Transfer detail, per-transfer reconciliation, orphan check.
    "CREATE INDEX ledger_entries_transfer_idx ON ledger_entries (transfer_id)",
    # Velocity and amount-spike rules, Q&A "sent".
    """
    CREATE INDEX transfers_from_created_idx
        ON transfers (from_account_id, created_at) INCLUDE (amount, status, type)
    """,
    # New-payee rule.
    """
    CREATE INDEX transfers_completed_pair_idx
        ON transfers (from_account_id, to_account_id) WHERE status = 'completed'
    """,
    # Q&A "received".
    "CREATE INDEX transfers_to_created_idx ON transfers (to_account_id, created_at)",
    # Admin risk queue.
    "CREATE INDEX transfers_held_created_idx ON transfers (created_at) WHERE status = 'held'",
    # Transfer detail.
    """
    CREATE INDEX transfer_status_history_transfer_idx
        ON transfer_status_history (transfer_id)
    """,
]

DOWNGRADE = [
    "DROP TABLE transfer_status_history",
    "DROP TABLE holds",
    "DROP TABLE ledger_entries",
    "DROP TABLE transfers",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
