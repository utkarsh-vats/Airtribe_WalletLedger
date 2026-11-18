"""deferred per-transfer balance check on ledger_entries and transfers

DESIGN.md §3.3 "Every transfer balances", docs/schema.md §4 (Q4). One shared function checks a
transfer at commit:
  (a) status completed or reversed: at least one entry, and Σ debits and Σ credits each equal
      transfers.amount;
  (b) status held or rejected: zero entries.
No limit on entry count. Violations raise SQLSTATE 23514 (check_violation) naming the transfer
and the expected vs actual sums, so the app can map it.

Two DEFERRABLE INITIALLY DEFERRED constraint triggers call it: one per inserted entry, one per
inserted transfer or status change. Deferral lets a service insert the transfer, its entries and
its status change in any order inside one transaction; only the state at COMMIT counts.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    # Runs as the caller (wallet_app has SELECT on both tables); search_path pinned so a
    # caller-controlled schema can't shadow the tables.
    """
    CREATE FUNCTION assert_transfer_balanced(p_transfer_id BIGINT) RETURNS void
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
    AS $$
    DECLARE
        v_status  TEXT;
        v_amount  BIGINT;
        v_entries BIGINT;
        v_debits  NUMERIC;
        v_credits NUMERIC;
    BEGIN
        SELECT status, amount INTO v_status, v_amount
          FROM transfers WHERE id = p_transfer_id;
        IF NOT FOUND THEN
            RETURN;  -- the FK on ledger_entries.transfer_id already rejects this
        END IF;

        SELECT count(*),
               coalesce(sum(amount) FILTER (WHERE direction = 'debit'), 0),
               coalesce(sum(amount) FILTER (WHERE direction = 'credit'), 0)
          INTO v_entries, v_debits, v_credits
          FROM ledger_entries WHERE transfer_id = p_transfer_id;

        IF v_status IN ('completed', 'reversed') THEN
            IF v_entries = 0 OR v_debits <> v_amount OR v_credits <> v_amount THEN
                RAISE EXCEPTION
                    'transfer % (%) is unbalanced: expected debits = credits = %, '
                    'got debits = %, credits = % across % entries',
                    p_transfer_id, v_status, v_amount, v_debits, v_credits, v_entries
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSIF v_entries <> 0 THEN  -- held or rejected
            RAISE EXCEPTION
                'transfer % (%) must have no entries: expected 0, '
                'got % (debits = %, credits = %)',
                p_transfer_id, v_status, v_entries, v_debits, v_credits
                USING ERRCODE = 'check_violation';
        END IF;
    END
    $$
    """,
    """
    CREATE FUNCTION ledger_entries_balance_trigger() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
    AS $$
    BEGIN
        PERFORM assert_transfer_balanced(NEW.transfer_id);
        RETURN NULL;
    END
    $$
    """,
    """
    CREATE FUNCTION transfers_balance_trigger() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
    AS $$
    BEGIN
        PERFORM assert_transfer_balanced(NEW.id);
        RETURN NULL;
    END
    $$
    """,
    """
    CREATE CONSTRAINT TRIGGER ledger_entries_balance_check
        AFTER INSERT ON ledger_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION ledger_entries_balance_trigger()
    """,
    """
    CREATE CONSTRAINT TRIGGER transfers_balance_check
        AFTER INSERT OR UPDATE OF status ON transfers
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION transfers_balance_trigger()
    """,
]

DOWNGRADE = [
    "DROP TRIGGER transfers_balance_check ON transfers",
    "DROP TRIGGER ledger_entries_balance_check ON ledger_entries",
    "DROP FUNCTION transfers_balance_trigger()",
    "DROP FUNCTION ledger_entries_balance_trigger()",
    "DROP FUNCTION assert_transfer_balanced(BIGINT)",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
