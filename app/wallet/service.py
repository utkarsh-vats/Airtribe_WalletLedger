"""Wallet services. Each public function owns its transaction (DESIGN.md §2)."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth.passwords import hash_password
from app.db import UNIQUE_VIOLATION, constraint_violated, transaction
from app.errors import ConflictError
from app.wallet import repository
from app.wallet.schemas import CreateUserRequest, UserResponse

_UNIQUE_USER_FIELDS = {
    "users_email_lower_key": ("EMAIL_ALREADY_REGISTERED", "That email is already registered."),
    "users_phone_key": ("PHONE_ALREADY_REGISTERED", "That phone number is already registered."),
}


async def create_user(conn: AsyncConnection, request: CreateUserRequest) -> UserResponse:
    # Hash before BEGIN: argon2 is slow, and no transaction should stay open while it runs.
    password_hash = await hash_password(request.password)
    try:
        async with transaction(conn):
            row = await repository.insert_user(
                conn,
                name=request.name,
                email=request.email,
                phone=request.phone,
                password_hash=password_hash,
            )
    except IntegrityError as exc:
        constraint = constraint_violated(exc, UNIQUE_VIOLATION)
        if constraint in _UNIQUE_USER_FIELDS:
            code, message = _UNIQUE_USER_FIELDS[constraint]
            raise ConflictError(message, code=code) from None
        raise
    return UserResponse.model_validate(row)
