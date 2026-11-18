"""let wallet_app update users.password_hash (rehash on login)

Login upgrades a stored argon2 hash made with older parameters to the current ones after a
successful verify (app/auth/passwords.py needs_rehash). That is an UPDATE of password_hash by
the runtime role; every other users column except kyc_status and updated_at stays read-only.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT UPDATE (password_hash) ON users TO wallet_app")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (password_hash) ON users FROM wallet_app")
