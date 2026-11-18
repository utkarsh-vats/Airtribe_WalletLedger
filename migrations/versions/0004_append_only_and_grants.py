"""append-only triggers on ledger_entries and transfer_status_history; wallet_app grants

DESIGN.md §3.3. Two layers:
- Triggers reject UPDATE, DELETE and TRUNCATE on the two append-only tables for every role,
  including the owner. Only a superuser with session_replication_role = replica bypasses them
  (the corruption tests do this on purpose).
- Grants: wallet_app gets only what the services need. No INSERT on transfer_status_history:
  its rows are written solely by the status-history trigger (0006).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("ledger_entries", "transfer_status_history")

UPGRADE = [
    """
    CREATE FUNCTION reject_modification() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
            USING HINT = 'Correct a posting with a new reversing transfer, never by editing rows.';
    END
    $$
    """,
    *(
        statement
        for table in APPEND_ONLY_TABLES
        for statement in (
            f"""
            CREATE TRIGGER {table}_reject_update_delete
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_modification()
            """,
            f"""
            CREATE TRIGGER {table}_reject_truncate
                BEFORE TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION reject_modification()
            """,
        )
    ),
    # Least privilege. Column-level UPDATE also lets the app lock rows with SELECT ... FOR UPDATE,
    # which needs UPDATE on at least one column.
    "GRANT SELECT, INSERT ON transfers TO wallet_app",
    "GRANT UPDATE (status, updated_at) ON transfers TO wallet_app",
    "GRANT SELECT, INSERT ON ledger_entries TO wallet_app",
    "GRANT SELECT, INSERT ON holds TO wallet_app",
    "GRANT UPDATE (status, resolved_at, resolved_by) ON holds TO wallet_app",
    "GRANT SELECT ON transfer_status_history TO wallet_app",
]

DOWNGRADE = [
    "REVOKE ALL ON transfer_status_history FROM wallet_app",
    "REVOKE ALL ON holds FROM wallet_app",
    "REVOKE ALL ON ledger_entries FROM wallet_app",
    "REVOKE ALL ON transfers FROM wallet_app",
    *(
        f"DROP TRIGGER {table}_{suffix} ON {table}"
        for table in APPEND_ONLY_TABLES
        for suffix in ("reject_truncate", "reject_update_delete")
    ),
    "DROP FUNCTION reject_modification()",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
