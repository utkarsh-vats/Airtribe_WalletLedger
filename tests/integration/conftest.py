from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import get_settings
from app.db import create_engine
from app.main import create_app
from tests.fakes import FixedClock


@pytest.fixture(scope="session")
def migrated_test_db() -> Iterator[None]:
    """Upgrade the test database to head once per run.

    Sync on purpose: migrations/env.py calls asyncio.run(), which can't nest inside the test
    event loop. tests/conftest.py has already pointed MIGRATION_DATABASE_URL at the test DB.
    """
    settings = get_settings()
    assert settings.migration_database_url == settings.test_migration_database_url
    command.upgrade(Config("alembic.ini"), "head")
    yield


@pytest.fixture(scope="session")
async def engine(migrated_test_db: None) -> AsyncIterator[AsyncEngine]:
    """Engine against the test database. tests/conftest.py has already pointed DATABASE_URL at
    TEST_DATABASE_URL; the assert is a second line of defence."""
    settings = get_settings()
    assert settings.database_url == settings.test_database_url
    eng = create_engine(settings)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def conn(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A fresh pooled connection with no transaction open, like `get_conn` gives a request."""
    async with engine.connect() as c:
        yield c


@pytest.fixture(scope="session")
async def owner_engine(migrated_test_db: None) -> AsyncIterator[AsyncEngine]:
    """Engine connected as wallet_owner, for schema checks and set-up the app role can't do.
    MIGRATION_DATABASE_URL already points at the test database (tests/conftest.py)."""
    settings = get_settings()
    assert settings.migration_database_url == settings.test_migration_database_url
    eng = create_async_engine(settings.migration_database_url)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def owner_tx(owner_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """wallet_owner connection inside a transaction that is always rolled back."""
    async with owner_engine.connect() as c:
        trans = await c.begin()
        try:
            yield c
        finally:
            await trans.rollback()


# --- API tests ---------------------------------------------------------------------------------

# Every user an API test creates uses this email domain, so cleanup can find them. API tests
# commit real rows (they go through the service's own transaction), unlike the schema tests.
API_TEST_EMAIL_DOMAIN = "api-tests.example.com"


# The app's clock in API tests: a fixed instant, never the wall clock (DESIGN.md §11).
API_TEST_NOW = datetime(2030, 1, 15, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(API_TEST_NOW)


@pytest.fixture
async def api(
    engine: AsyncEngine, owner_engine: AsyncEngine, clock: FixedClock
) -> AsyncIterator[AsyncClient]:
    """HTTP client for the real app against the test database, lifespan included.

    Afterwards, users created under API_TEST_EMAIL_DOMAIN are deleted as wallet_owner
    (wallet_app has no DELETE). Tests that later create wallets or transfers will need the
    superuser reset fixture planned for 2.9 instead.
    """
    app = create_app(get_settings(), clock=clock)
    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            yield client
    finally:
        pattern = {"pattern": f"%@{API_TEST_EMAIL_DOMAIN}"}
        async with owner_engine.begin() as owner:
            # Wallets reference their user. No transfers exist yet in API tests; once they do,
            # this must become the superuser reset planned for 2.9 (append-only tables).
            await owner.execute(
                text(
                    "DELETE FROM accounts WHERE user_id IN "
                    "(SELECT id FROM users WHERE email LIKE :pattern)"
                ),
                pattern,
            )
            await owner.execute(text("DELETE FROM users WHERE email LIKE :pattern"), pattern)
