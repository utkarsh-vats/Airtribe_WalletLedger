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


async def update_kyc_status(
    conn: AsyncConnection, *, user_id: int, kyc_status: str
) -> dict[str, object] | None:
    row = (
        await conn.execute(
            text(
                "UPDATE users SET kyc_status = :k, updated_at = now() WHERE id = :id "
                "RETURNING id, name, email, phone, role, kyc_status, created_at"
            ),
            {"k": kyc_status, "id": user_id},
        )
    ).one_or_none()
    return None if row is None else dict(row._mapping)
