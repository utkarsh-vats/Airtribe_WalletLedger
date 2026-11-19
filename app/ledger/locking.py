"""Row locks for the money path (DESIGN.md §4).

Every service that moves money locks every account it touches, and only through
`lock_accounts`. That one function always locks in ascending `accounts.id` order, so no two
transactions can wait on each other in a cycle: whoever holds the lowest contested id goes
first, and the other one waits for it instead of holding a row it needs.

At READ COMMITTED, a `SELECT … FOR UPDATE` that had to wait returns the row as last committed,
so the balances in the returned `LockedAccounts` are current. They stay current until this
transaction ends, because nobody else can change those rows meanwhile.

The result is also the proof of locking: posting (step 2.5b) accepts only accounts found in a
`LockedAccounts` taken in the same transaction.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Transaction
from sqlalchemy.ext.asyncio import AsyncConnection

# `ORDER BY id` sits below the row-locking step in the plan (LockRows over Sort), so rows are
# locked in id order, one at a time, whatever order the caller listed them in.
_LOCK_SQL = text(
    "SELECT id, user_id, account_type, currency, balance, held_amount "
    "FROM accounts WHERE id = ANY(:ids) ORDER BY id FOR UPDATE"
)


class AccountNotLockedError(LookupError):
    """A programming error: the account wasn't locked in this transaction."""


@dataclass(frozen=True, slots=True)
class LockedAccount:
    """An `accounts` row as it was when locked. Amounts in paise."""

    id: int
    user_id: int | None
    account_type: str
    currency: str
    balance: int
    held_amount: int

    @property
    def available(self) -> int:
        return self.balance - self.held_amount

    @property
    def is_user_wallet(self) -> bool:
        return self.account_type == "user_wallet"


def lock_order(ids: Iterable[int]) -> tuple[int, ...]:
    """The order `lock_accounts` locks in: distinct ids, ascending."""
    ids = tuple(ids)
    if not ids:
        raise ValueError("lock_accounts needs at least one account id")
    for account_id in ids:
        # bool is an int subclass; True would silently mean account 1.
        if type(account_id) is not int or account_id <= 0:
            raise ValueError(f"account ids must be positive integers, got {account_id!r}")
    return tuple(sorted(set(ids)))


class LockedAccounts(Mapping[int, LockedAccount]):
    """The accounts locked by one `lock_accounts` call, keyed by id.

    Ids that were requested but don't exist are in `missing`; the caller decides what that
    means (usually 404). Read-only, and tied to the transaction that took the locks.
    """

    def __init__(
        self,
        rows: Iterable[LockedAccount],
        *,
        requested: tuple[int, ...],
        transaction: Transaction | None,
    ) -> None:
        self._rows = {row.id: row for row in rows}
        self.requested = requested
        self.missing = frozenset(requested) - self._rows.keys()
        self._transaction = transaction

    def __getitem__(self, account_id: int) -> LockedAccount:
        return self._rows[account_id]

    def __iter__(self) -> Iterator[int]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def require(self, conn: AsyncConnection, account_id: int) -> LockedAccount:
        """The locked row for `account_id`, if it was locked in `conn`'s current transaction.

        Raises AccountNotLockedError otherwise: posting to an account without its lock is the
        exact bug `scripts/naive_transfer_demo.py` shows, so it fails loudly instead.
        """
        if self._transaction is None or conn.sync_connection is None:
            raise AccountNotLockedError("these locks were not taken in a transaction")
        if conn.sync_connection.get_transaction() is not self._transaction:
            raise AccountNotLockedError("these locks belong to a different transaction")
        try:
            return self._rows[account_id]
        except KeyError:
            raise AccountNotLockedError(f"account {account_id} is not locked") from None


async def lock_accounts(conn: AsyncConnection, ids: Iterable[int]) -> LockedAccounts:
    """`SELECT … FOR UPDATE` the given accounts, in ascending id order, and return them.

    Must run inside the service's transaction (`app.db.transaction`); the locks are held until
    it commits or rolls back. Blocks while another transaction holds any of the rows, up to the
    transaction's `lock_timeout` (none by default).
    """
    order = lock_order(ids)
    if not conn.in_transaction() or conn.sync_connection is None:
        raise RuntimeError("lock_accounts must run inside a transaction")
    result = await conn.execute(_LOCK_SQL, {"ids": list(order)})
    rows = [LockedAccount(**row._mapping) for row in result]
    return LockedAccounts(rows, requested=order, transaction=conn.sync_connection.get_transaction())
