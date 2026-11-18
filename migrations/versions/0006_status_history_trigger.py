"""status-history trigger on transfers (SECURITY DEFINER)

DESIGN.md §3.3 "Every status change audited" and "Audit log append-only". Every transfer
insert, and every real status change, writes one transfer_status_history row:
  - from_status: NULL on insert, else the old status
  - changed_by:  current_setting('app.actor_id', true); empty/unset means the system (NULL)
  - reason:      current_setting('app.status_reason', true); empty/unset means NULL
Both settings are transaction-local, set by the service via set_config(..., true).

The function is SECURITY DEFINER, owned by wallet_owner, with search_path pinned and EXECUTE
revoked from PUBLIC. wallet_app has no INSERT on the history table (0004), so the only way a
history row appears is a real status change on transfers.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE FUNCTION record_transfer_status_change() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
    AS $$
    BEGIN
        INSERT INTO public.transfer_status_history
            (transfer_id, from_status, to_status, changed_by, reason)
        VALUES (
            NEW.id,
            CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END,
            NEW.status,
            nullif(current_setting('app.actor_id', true), '')::BIGINT,
            nullif(current_setting('app.status_reason', true), '')
        );
        RETURN NULL;
    END
    $$
    """,
    # Trigger functions can't be called directly, but keep EXECUTE off PUBLIC anyway: a
    # SECURITY DEFINER function should only ever run through its triggers.
    "REVOKE ALL ON FUNCTION record_transfer_status_change() FROM PUBLIC",
    """
    CREATE TRIGGER transfers_status_history_insert
        AFTER INSERT ON transfers
        FOR EACH ROW EXECUTE FUNCTION record_transfer_status_change()
    """,
    # UPDATE OF status fires even when status is set to its current value; WHEN skips those.
    """
    CREATE TRIGGER transfers_status_history_update
        AFTER UPDATE OF status ON transfers
        FOR EACH ROW
        WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION record_transfer_status_change()
    """,
]

DOWNGRADE = [
    "DROP TRIGGER transfers_status_history_update ON transfers",
    "DROP TRIGGER transfers_status_history_insert ON transfers",
    "DROP FUNCTION record_transfer_status_change()",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
