"""column-level INSERT grants for wallet_app on users and accounts

Until now wallet_app had table-level INSERT on both tables, so at the database level it could
create an admin (users.role) or a pre-verified user (users.kyc_status), or open a wallet that
already holds money (accounts.balance / held_amount). The API never sent those columns; now the
database refuses them too (42501):

  - users:    INSERT (name, email, phone, password_hash). role and kyc_status always take their
              defaults ('user', 'pending'); admins come only from the owner-run seed.
  - accounts: INSERT (user_id, account_type, label). balance and held_amount always start at 0,
              so a wallet gains money only through ledger postings; currency takes its default
              ('INR', fixed by CHECK).

System accounts stay protected without a grant change: the three exist from 0002, the partial
unique index allows one per type, and accounts_owner_check ties user_id to account_type.

A table-level REVOKE also removes column-level grants of that privilege, so downgrade revokes
first and then restores the table-level grant 0002 gave.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    "REVOKE INSERT ON users FROM wallet_app",
    "GRANT INSERT (name, email, phone, password_hash) ON users TO wallet_app",
    "REVOKE INSERT ON accounts FROM wallet_app",
    "GRANT INSERT (user_id, account_type, label) ON accounts TO wallet_app",
]

DOWNGRADE = [
    "REVOKE INSERT ON accounts FROM wallet_app",
    "GRANT INSERT ON accounts TO wallet_app",
    "REVOKE INSERT ON users FROM wallet_app",
    "GRANT INSERT ON users TO wallet_app",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
