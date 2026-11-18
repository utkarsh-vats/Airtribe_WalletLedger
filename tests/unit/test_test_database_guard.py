import pytest

from app.config import Settings
from tests.conftest import _check_test_database

BASE = "postgresql+asyncpg://u:p@localhost:5433/"
GOOD = {
    "database_url": BASE + "walletledger",
    "test_database_url": BASE + "walletledger_test",
    "migration_database_url": BASE + "walletledger",
    "test_migration_database_url": BASE + "walletledger_test",
}


def test_accepts_separate_test_databases() -> None:
    _check_test_database(Settings(**GOOD))


@pytest.mark.parametrize("field", ["test_database_url", "test_migration_database_url"])
@pytest.mark.parametrize(
    "bad_url",
    [
        BASE + "walletledger",  # same as dev
        BASE + "walletledger_testing",  # wrong suffix
        BASE,  # no database
    ],
)
def test_rejects_unsafe_test_database(field: str, bad_url: str) -> None:
    with pytest.raises(pytest.UsageError):
        _check_test_database(Settings(**{**GOOD, field: bad_url}))
