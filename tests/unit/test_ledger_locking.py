"""app/ledger/locking.py: lock order, argument checks and the LockedAccounts mapping.

The locking itself needs Postgres: tests/integration/test_ledger_locking.py.
"""

import pytest

from app.ledger.locking import AccountNotLockedError, LockedAccount, LockedAccounts, lock_order


def _account(account_id: int, balance: int = 0, held: int = 0) -> LockedAccount:
    return LockedAccount(
        id=account_id,
        user_id=7,
        account_type="user_wallet",
        currency="INR",
        balance=balance,
        held_amount=held,
    )


@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        ([5], (5,)),
        ([9, 2, 5], (2, 5, 9)),
        ([3, 1, 3, 1], (1, 3)),  # duplicates locked once: same-row transfers are caught later
        ((x for x in [4, 2]), (2, 4)),  # any iterable
    ],
)
def test_lock_order_is_distinct_and_ascending(ids: list[int], expected: tuple[int, ...]) -> None:
    assert lock_order(ids) == expected


@pytest.mark.parametrize(
    "ids", [[], [0], [-1], [1, True], [1.0], ["1"], [None]], ids=lambda v: repr(v)
)
def test_lock_order_rejects_bad_ids(ids: list[object]) -> None:
    with pytest.raises(ValueError, match="account id"):
        lock_order(ids)  # type: ignore[arg-type]


def test_available_is_balance_minus_held() -> None:
    account = _account(1, balance=10_000, held=2_500)

    assert account.available == 7_500
    assert account.is_user_wallet


def test_locked_accounts_reports_missing_ids() -> None:
    locked = LockedAccounts([_account(2), _account(5)], requested=(2, 5, 8), transaction=None)

    assert list(locked) == [2, 5]
    assert locked[5].id == 5
    assert locked.missing == {8}
    assert len(locked) == 2


def test_require_refuses_locks_taken_outside_a_transaction() -> None:
    locked = LockedAccounts([_account(2)], requested=(2,), transaction=None)

    with pytest.raises(AccountNotLockedError, match="not taken in a transaction"):
        locked.require(object(), 2)  # type: ignore[arg-type]
