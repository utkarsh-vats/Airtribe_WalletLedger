"""Session-wide test setup: tests must never touch the dev database.

Before any test is collected, the test URLs are validated, and DATABASE_URL /
MIGRATION_DATABASE_URL are replaced with their test counterparts for the whole run, so even
application code under test (create_app, get_settings, migrations/env.py) can only reach the
test database.
"""

import os

import pytest
from sqlalchemy.engine import make_url

from app.config import Settings, get_settings

# (dev setting, test setting, env var to override) — runtime URL and migration URL.
_URL_PAIRS = (
    ("database_url", "test_database_url", "DATABASE_URL"),
    ("migration_database_url", "test_migration_database_url", "MIGRATION_DATABASE_URL"),
)


def _check_test_database(settings: Settings) -> None:
    for dev_field, test_field, _ in _URL_PAIRS:
        test_url = make_url(getattr(settings, test_field))
        dev_url = make_url(getattr(settings, dev_field))
        name = test_field.upper()

        if not (test_url.database or "").endswith("_test"):
            raise pytest.UsageError(
                f"{name} must name a database ending in '_test', got {test_url.database!r}"
            )
        if (test_url.host, test_url.port, test_url.database) == (
            dev_url.host,
            dev_url.port,
            dev_url.database,
        ):
            raise pytest.UsageError(f"{name} points at the same database as {dev_field.upper()}")


def pytest_configure(config: pytest.Config) -> None:
    settings = Settings()
    _check_test_database(settings)
    # Environment variables win over .env in pydantic-settings.
    for _, test_field, env_var in _URL_PAIRS:
        os.environ[env_var] = getattr(settings, test_field)
    get_settings.cache_clear()
