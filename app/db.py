"""Database access: async engine, per-request connection, and the transaction helper.

Rules this module enforces:
- A request gets a pooled connection with no transaction open. Services start one with
  `transaction(...)`, so BEGIN/COMMIT live in the service layer, visible in one place.
- Session settings the database relies on (`app.actor_id` for the status-history trigger,
  `lock_timeout`) are set with `set_config(name, value, true)`, the parameterised equivalent of
  `SET LOCAL`: they last until the transaction ends and never leak to the next pool user.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import Settings

IsolationLevel = Literal["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]

# Whitelist: the isolation level is interpolated into SQL, so only these literals are allowed.
_ISOLATION_SQL: dict[str, str] = {
    "READ COMMITTED": "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
    "REPEATABLE READ": "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
    "SERIALIZABLE": "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
}


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
    )


async def get_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """FastAPI dependency: one pooled connection per request, no transaction open."""
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        yield conn


async def set_lock_timeout(conn: AsyncConnection, value: str) -> None:
    """Change lock_timeout for the rest of the current transaction (e.g. '2s', '0')."""
    await conn.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": value})


async def set_actor(conn: AsyncConnection, actor_id: int | None) -> None:
    """Record who is acting, for the transfer status-history trigger. NULL means system."""
    value = "" if actor_id is None else str(actor_id)
    await conn.execute(text("SELECT set_config('app.actor_id', :v, true)"), {"v": value})


@asynccontextmanager
async def transaction(
    conn: AsyncConnection,
    *,
    actor_id: int | None = None,
    lock_timeout: str | None = None,
    isolation: IsolationLevel = "READ COMMITTED",
) -> AsyncIterator[AsyncConnection]:
    """BEGIN … COMMIT on `conn`; rolls back if the block raises.

    The isolation level is set as the first statement of the transaction. `actor_id` and
    `lock_timeout` are transaction-local. Refuses to start if a transaction is already open,
    so a stray query before the service's BEGIN can't silently widen the transaction.
    """
    if conn.in_transaction():
        raise RuntimeError("transaction() called on a connection that is already in a transaction")

    async with conn.begin():
        await conn.execute(text(_ISOLATION_SQL[isolation]))
        if actor_id is not None:
            await set_actor(conn, actor_id)
        if lock_timeout is not None:
            await set_lock_timeout(conn, lock_timeout)
        yield conn
