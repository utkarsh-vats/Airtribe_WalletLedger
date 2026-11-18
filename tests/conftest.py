"""Session-wide test setup: tests must never touch the dev database.

Before any test is collected, TEST_DATABASE_URL is validated and DATABASE_URL is replaced
with it for the whole run, so even application code under test (create_app, get_settings)
can only reach the test database.
"""

import os

import pytest
from sqlalchemy.engine import make_url

from app.config import Settings, get_settings


def _check_test_database(settings: Settings) -> None:
    test_url = make_url(settings.test_database_url)
    dev_url = make_url(settings.database_url)

    if not (test_url.database or "").endswith("_test"):
        raise pytest.UsageError(
            f"TEST_DATABASE_URL must name a database ending in '_test', got {test_url.database!r}"
        )
    if (test_url.host, test_url.port, test_url.database) == (
        dev_url.host,
        dev_url.port,
        dev_url.database,
    ):
        raise pytest.UsageError("TEST_DATABASE_URL points at the same database as DATABASE_URL")


def pytest_configure(config: pytest.Config) -> None:
    settings = Settings()
    _check_test_database(settings)
    # Environment variables win over .env in pydantic-settings.
    os.environ["DATABASE_URL"] = settings.test_database_url
    get_settings.cache_clear()
