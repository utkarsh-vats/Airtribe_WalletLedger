"""Step 2.1d: the admin seed, run as wallet_owner like the migrate service does."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.auth.admin_seed import AdminSeedError, seed_admin
from app.auth.passwords import verify_password

PASSWORD = "a-long-admin-password"


@pytest.fixture
async def owner(owner_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """wallet_owner in a transaction that is rolled back: the seed's writes never persist."""
    async with owner_engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


def _email() -> str:
    return f"admin-{uuid.uuid4().hex[:8]}@example.com"


async def _admin_row(conn: AsyncConnection, email: str) -> tuple:
    return (
        await conn.execute(
            text("SELECT role, phone, password_hash FROM users WHERE email = :e"), {"e": email}
        )
    ).one()


async def test_creates_admin(owner: AsyncConnection) -> None:
    email = _email()

    outcome = await seed_admin(owner, email=email, password=PASSWORD, phone="+910000000000")

    role, phone, password_hash = await _admin_row(owner, email)
    assert outcome == "created"
    assert (role, phone) == ("admin", "+910000000000")
    assert await verify_password(password_hash, PASSWORD)


async def test_rerun_with_same_password_changes_nothing(owner: AsyncConnection) -> None:
    email = _email()
    await seed_admin(owner, email=email, password=PASSWORD, phone="+910000000000")
    before = await _admin_row(owner, email)

    outcome = await seed_admin(owner, email=email.upper(), password=PASSWORD, phone="+910000000000")

    assert outcome == "unchanged"
    assert await _admin_row(owner, email) == before


async def test_new_password_replaces_the_hash(owner: AsyncConnection) -> None:
    email = _email()
    await seed_admin(owner, email=email, password=PASSWORD, phone="+910000000000")

    outcome = await seed_admin(
        owner, email=email, password="another-long-password", phone="+910000000000"
    )

    _, _, password_hash = await _admin_row(owner, email)
    assert outcome == "password updated"
    assert await verify_password(password_hash, "another-long-password")
    assert not await verify_password(password_hash, PASSWORD)


async def test_refuses_to_promote_an_existing_user(owner: AsyncConnection) -> None:
    email = _email()
    await owner.execute(
        text(
            "INSERT INTO users (name, email, phone, password_hash) "
            "VALUES ('Someone', :e, '+919811111111', 'x')"
        ),
        {"e": email},
    )

    with pytest.raises(AdminSeedError, match="not an admin"):
        await seed_admin(owner, email=email, password=PASSWORD, phone="+910000000000")

    role, _, _ = await _admin_row(owner, email)
    assert role == "user"


async def test_short_password_is_refused(owner: AsyncConnection) -> None:
    with pytest.raises(AdminSeedError, match="at least 12"):
        await seed_admin(owner, email=_email(), password="short", phone="+910000000000")
