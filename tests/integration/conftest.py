from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import get_settings
from app.db import create_engine


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
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
