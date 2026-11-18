"""HTTP routes for authentication."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth import service
from app.auth.deps import get_app_settings
from app.auth.schemas import LoginRequest, TokenResponse
from app.clock import Clock, get_clock
from app.config import Settings
from app.db import get_conn

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    conn: Annotated[AsyncConnection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> TokenResponse:
    """Exchange email + password for a 60-minute bearer token. No Idempotency-Key (§5)."""
    return await service.login(conn, body, settings=settings, clock=clock)
