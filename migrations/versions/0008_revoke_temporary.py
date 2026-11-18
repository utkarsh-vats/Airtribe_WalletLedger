"""revoke TEMPORARY on the database from wallet_app

wallet_app had TEMPORARY only so the 0.2a transaction tests could use a temp table. Those tests
now use real tables (job_locks, users), so the runtime role keeps just CONNECT: it can't create
any object at all, temporary or not. The init script grants CONNECT only for fresh volumes; this
migration brings existing databases in line.

The database name differs between dev and test, so it comes from current_database(). The grant
was made by wallet_owner (the database owner), who runs migrations and can revoke it.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM wallet_app', current_database());
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO wallet_app', current_database());
        END
        $$
        """
    )
