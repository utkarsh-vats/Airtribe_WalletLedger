"""users and accounts, with the three system accounts

Tables, constraints and grants from DESIGN.md §3.2 and docs/schema.md §4. Constraint names are
explicit so the API can map a violation to an error code (e.g. users_email_lower_key → 409).

One statement per op.execute: asyncpg runs each as a prepared statement, which can't hold
several commands.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    r"""
    CREATE TABLE users (
        id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name          TEXT        NOT NULL,
        email         TEXT        NOT NULL,
        -- E.164; the API normalizes before insert, this rejects anything that bypasses it.
        phone         TEXT        NOT NULL,
        password_hash TEXT        NOT NULL,
        role          TEXT        NOT NULL DEFAULT 'user',
        kyc_status    TEXT        NOT NULL DEFAULT 'pending',
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT users_phone_key        UNIQUE (phone),
        CONSTRAINT users_phone_e164_check CHECK (phone ~ '^\+[1-9][0-9]{7,14}$'),
        CONSTRAINT users_role_check       CHECK (role IN ('user', 'admin')),
        CONSTRAINT users_kyc_status_check CHECK (kyc_status IN ('pending', 'verified', 'rejected'))
    )
    """,
    # Email is not a key (PII, can change); uniqueness is case-insensitive.
    "CREATE UNIQUE INDEX users_email_lower_key ON users (lower(email))",
    """
    CREATE TABLE accounts (
        id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id      BIGINT      REFERENCES users (id),
        account_type TEXT        NOT NULL,
        label        TEXT        NOT NULL,
        currency     CHAR(3)     NOT NULL DEFAULT 'INR',
        balance      BIGINT      NOT NULL DEFAULT 0,   -- paise; = sum(credits) - sum(debits)
        held_amount  BIGINT      NOT NULL DEFAULT 0,   -- paise; = sum(active holds)
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

        CONSTRAINT accounts_account_type_check CHECK (account_type IN (
            'user_wallet', 'system_cash_in', 'system_cash_out', 'system_fee_revenue'
        )),
        CONSTRAINT accounts_currency_check     CHECK (currency = 'INR'),
        -- Wallets belong to a user; system accounts belong to nobody.
        CONSTRAINT accounts_owner_check        CHECK (
            (account_type = 'user_wallet') = (user_id IS NOT NULL)
        ),
        CONSTRAINT accounts_held_amount_check  CHECK (held_amount >= 0),
        -- Available balance never negative for wallets (implies balance >= 0).
        -- System accounts are exempt: cash_in runs negative by design.
        CONSTRAINT accounts_available_check    CHECK (
            account_type <> 'user_wallet' OR balance - held_amount >= 0
        ),
        -- Also serves as the index for the user_id foreign key (leading column).
        CONSTRAINT accounts_user_label_key     UNIQUE (user_id, label)
    )
    """,
    # Exactly one row per system account type.
    """
    CREATE UNIQUE INDEX accounts_system_type_key ON accounts (account_type)
        WHERE user_id IS NULL
    """,
    # Seeded here so the system accounts get the lowest ids (1, 2, 3).
    """
    INSERT INTO accounts (account_type, label) VALUES
        ('system_cash_in',     'cash_in'),
        ('system_cash_out',    'cash_out'),
        ('system_fee_revenue', 'fee_revenue')
    """,
    # Least privilege for the runtime role: no DELETE, UPDATE only on columns the app changes.
    "GRANT SELECT, INSERT ON users TO wallet_app",
    "GRANT UPDATE (kyc_status, updated_at) ON users TO wallet_app",
    "GRANT SELECT, INSERT ON accounts TO wallet_app",
    "GRANT UPDATE (balance, held_amount, updated_at) ON accounts TO wallet_app",
]

DOWNGRADE = [
    "DROP TABLE accounts",
    "DROP TABLE users",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
