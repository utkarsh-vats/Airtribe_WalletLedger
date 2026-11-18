"""HTTP routes for users and wallets. Thin: parse, call the service, shape the response."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import get_conn
from app.wallet import service
from app.wallet.schemas import CreateUserRequest, UserResponse

router = APIRouter(tags=["users"])

Conn = Annotated[AsyncConnection, Depends(get_conn)]


@router.post("/users", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def create_user(body: CreateUserRequest, conn: Conn) -> UserResponse:
    """Register a user. New users are role `user` and KYC `pending` (DESIGN.md §6)."""
    return await service.create_user(conn, body)
