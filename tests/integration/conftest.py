from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import get_settings
from app.db import create_engine


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    """Engine against the real Postgres from DATABASE_URL (compose: localhost:5433)."""
    eng = create_engine(get_settings())
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def conn(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A fresh pooled connection with no transaction open, like `get_conn` gives a request."""
    async with engine.connect() as c:
        yield c
