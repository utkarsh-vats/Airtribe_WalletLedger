"""SQL for authentication: one statement per function, no transaction handling here."""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth.tokens import Role


@dataclass(frozen=True, slots=True)
class LoginUser:
    id: int
    role: Role
    password_hash: str


async def find_login_user(conn: AsyncConnection, email: str) -> LoginUser | None:
    """Case-insensitive lookup; served by the users_email_lower_key unique index."""
    row = (
        await conn.execute(
            text("SELECT id, role, password_hash FROM users WHERE lower(email) = lower(:email)"),
            {"email": email},
        )
    ).one_or_none()
    return None if row is None else LoginUser(row.id, row.role, row.password_hash)


async def replace_password_hash(
    conn: AsyncConnection, *, user_id: int, old_hash: str, new_hash: str
) -> bool:
    """Compare-and-set: only replaces the hash that was verified. False if it changed since."""
    result = await conn.execute(
        text(
            "UPDATE users SET password_hash = :new, updated_at = now() "
            "WHERE id = :id AND password_hash = :old"
        ),
        {"id": user_id, "old": old_hash, "new": new_hash},
    )
    return result.rowcount == 1
