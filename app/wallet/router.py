"""HTTP routes for users and wallets. Thin: parse, call the service, shape the response."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth.deps import AdminUser
from app.db import get_conn
from app.wallet import service
from app.wallet.schemas import CreateUserRequest, SetKycRequest, UserResponse

router = APIRouter(tags=["users"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])

Conn = Annotated[AsyncConnection, Depends(get_conn)]


@router.post("/users", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def create_user(body: CreateUserRequest, conn: Conn) -> UserResponse:
    """Register a user. New users are role `user` and KYC `pending` (DESIGN.md §6)."""
    return await service.create_user(conn, body)


@admin_router.post("/users/{user_id}/kyc", response_model=UserResponse)
async def set_kyc_status(
    user_id: Annotated[int, Path(gt=0)], body: SetKycRequest, conn: Conn, admin: AdminUser
) -> UserResponse:
    """Set a user's KYC status (admin only). Withdrawals require `verified` (DESIGN.md §4.2)."""
    return await service.set_kyc_status(conn, user_id, body, admin_id=admin.id)
