"""HTTP routes for users and wallets. Thin: parse, call the service, shape the response."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth.deps import AdminUser, CurrentUser, require_self
from app.db import get_conn
from app.wallet import service
from app.wallet.schemas import (
    BalanceResponse,
    CreateUserRequest,
    CreateWalletRequest,
    SetKycRequest,
    UserResponse,
    WalletResponse,
)

router = APIRouter(tags=["users"])
wallet_router = APIRouter(tags=["wallets"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])

Conn = Annotated[AsyncConnection, Depends(get_conn)]
PositiveId = Annotated[int, Path(gt=0)]


@router.post("/users", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def create_user(body: CreateUserRequest, conn: Conn) -> UserResponse:
    """Register a user. New users are role `user` and KYC `pending` (DESIGN.md §6)."""
    return await service.create_user(conn, body)


@admin_router.post("/users/{user_id}/kyc", response_model=UserResponse)
async def set_kyc_status(
    user_id: PositiveId, body: SetKycRequest, conn: Conn, admin: AdminUser
) -> UserResponse:
    """Set a user's KYC status (admin only). Withdrawals require `verified` (DESIGN.md §4.2)."""
    return await service.set_kyc_status(conn, user_id, body, admin_id=admin.id)


@router.post(
    "/users/{user_id}/wallets",
    status_code=status.HTTP_201_CREATED,
    response_model=WalletResponse,
    tags=["wallets"],
)
async def create_wallet(
    user_id: PositiveId, conn: Conn, user: CurrentUser, body: CreateWalletRequest | None = None
) -> WalletResponse:
    """Open a wallet. Only for yourself. Body optional: label defaults to `main`."""
    require_self(user, user_id)
    return await service.create_wallet(conn, user_id, body or CreateWalletRequest())


@wallet_router.get("/wallets/{wallet_id}/balance", response_model=BalanceResponse)
async def get_balance(wallet_id: PositiveId, conn: Conn, user: CurrentUser) -> BalanceResponse:
    """Balance, held and available, in paise. Your own wallets, or any wallet for an admin."""
    return await service.get_balance(conn, wallet_id, caller=user)
