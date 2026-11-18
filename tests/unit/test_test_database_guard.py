import pytest

from app.config import Settings
from tests.conftest import _check_test_database

DEV = "postgresql+asyncpg://u:p@localhost:5433/walletledger"


def _settings(test_url: str) -> Settings:
    return Settings(database_url=DEV, test_database_url=test_url)


def test_accepts_separate_test_database() -> None:
    _check_test_database(_settings("postgresql+asyncpg://u:p@localhost:5433/walletledger_test"))


@pytest.mark.parametrize(
    "test_url",
    [
        DEV,  # same database
        "postgresql+asyncpg://u:p@localhost:5433/walletledger_testing",  # wrong suffix
        "postgresql+asyncpg://u:p@localhost:5433/",  # no database
    ],
)
def test_rejects_unsafe_test_database(test_url: str) -> None:
    with pytest.raises(pytest.UsageError):
        _check_test_database(_settings(test_url))
