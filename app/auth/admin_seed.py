"""Seed the admin user from ADMIN_EMAIL / ADMIN_PASSWORD (DESIGN.md §6).

Run by the compose `migrate` service after `alembic upgrade head`, as wallet_owner (the runtime
role can't create admins). Safe to run on every start:

  - ADMIN_EMAIL or ADMIN_PASSWORD unset  -> skipped (a fresh clone still starts)
  - no user with that email               -> admin created
  - admin exists, password still verifies -> unchanged
  - admin exists, password differs        -> hash replaced (the environment is the source of truth)
  - a non-admin has that email            -> error; never silently promote an existing user

    python -m app.auth.admin_seed
"""

import asyncio
import logging
import sys
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.auth.passwords import hash_password, verify_password
from app.config import get_settings
from app.wallet.phone import normalize_phone

logger = logging.getLogger("app.auth.admin_seed")

MIN_ADMIN_PASSWORD_LENGTH = 12
Outcome = Literal["created", "unchanged", "password updated"]


class AdminSeedError(Exception):
    pass


async def seed_admin(conn: AsyncConnection, *, email: str, password: str, phone: str) -> Outcome:
    """Create or reconcile the admin. Runs inside the caller's transaction."""
    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        raise AdminSeedError(
            f"ADMIN_PASSWORD must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters"
        )

    existing = (
        await conn.execute(
            text(
                "SELECT id, role, password_hash FROM users "
                "WHERE lower(email) = lower(:email) FOR UPDATE"
            ),
            {"email": email},
        )
    ).one_or_none()

    if existing is None:
        await conn.execute(
            text(
                "INSERT INTO users (name, email, phone, password_hash, role) "
                "VALUES ('Admin', :email, :phone, :hash, 'admin')"
            ),
            {
                "email": email,
                "phone": normalize_phone(phone),
                "hash": await hash_password(password),
            },
        )
        return "created"

    if existing.role != "admin":
        raise AdminSeedError(
            f"user {existing.id} with ADMIN_EMAIL is not an admin; refusing to promote it"
        )
    if await verify_password(existing.password_hash, password):
        return "unchanged"
    await conn.execute(
        text("UPDATE users SET password_hash = :hash, updated_at = now() WHERE id = :id"),
        {"hash": await hash_password(password), "id": existing.id},
    )
    return "password updated"


async def _main() -> int:
    settings = get_settings()
    email = settings.admin_email.strip()
    password = settings.admin_password.get_secret_value()
    if not email or not password:
        logger.info("admin seed skipped: ADMIN_EMAIL / ADMIN_PASSWORD not set")
        return 0

    engine = create_async_engine(settings.migration_database_url)
    try:
        async with engine.begin() as conn:
            role = await conn.scalar(text("SELECT current_user"))
            if role != "wallet_owner":
                raise AdminSeedError(f"must run as wallet_owner, connected as {role}")
            outcome = await seed_admin(
                conn, email=email, password=password, phone=settings.admin_phone
            )
    except AdminSeedError as exc:
        logger.error("admin seed failed: %s", exc)
        return 1
    finally:
        await engine.dispose()
    logger.info("admin seed: %s (%s)", outcome, email)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  [%(name)s] %(message)s")
    sys.exit(asyncio.run(_main()))
