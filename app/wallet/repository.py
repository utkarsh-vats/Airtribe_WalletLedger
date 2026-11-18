"""SQL for the wallet API: one statement per function, no transaction handling here."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def insert_user(
    conn: AsyncConnection, *, name: str, email: str, phone: str, password_hash: str
) -> dict[str, object]:
    row = (
        await conn.execute(
            text(
                "INSERT INTO users (name, email, phone, password_hash) "
                "VALUES (:name, :email, :phone, :password_hash) "
                "RETURNING id, name, email, phone, role, kyc_status, created_at"
            ),
            {"name": name, "email": email, "phone": phone, "password_hash": password_hash},
        )
    ).one()
    return dict(row._mapping)
