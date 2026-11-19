"""Wallet services. Each public function owns its transaction (DESIGN.md §2)."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth.passwords import hash_password
from app.auth.tokens import Principal
from app.db import FOREIGN_KEY_VIOLATION, UNIQUE_VIOLATION, constraint_violated, transaction
from app.errors import ConflictError, ForbiddenError, NotFoundError
from app.wallet import repository
from app.wallet.schemas import (
    BalanceResponse,
    CreateUserRequest,
    CreateWalletRequest,
    SetKycRequest,
    UserResponse,
    WalletResponse,
)

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


async def set_kyc_status(
    conn: AsyncConnection, user_id: int, request: SetKycRequest, *, admin_id: int
) -> UserResponse:
    """Admin sets a user's KYC status (DESIGN.md §6). Any status may follow any other."""
    async with transaction(conn, actor_id=admin_id):
        row = await repository.update_kyc_status(
            conn, user_id=user_id, kyc_status=request.kyc_status
        )
    if row is None:
        raise NotFoundError("User not found.", code="USER_NOT_FOUND")
    return UserResponse.model_validate(row)


async def create_wallet(
    conn: AsyncConnection, user_id: int, request: CreateWalletRequest
) -> WalletResponse:
    """Open a wallet for `user_id`. The router has already checked the caller is that user."""
    try:
        async with transaction(conn):
            row = await repository.insert_wallet(conn, user_id=user_id, label=request.label)
    except IntegrityError as exc:
        if constraint_violated(exc, UNIQUE_VIOLATION) == "accounts_user_label_key":
            raise ConflictError(
                f"You already have a wallet labelled {request.label!r}.",
                code="WALLET_LABEL_TAKEN",
            ) from None
        if constraint_violated(exc, FOREIGN_KEY_VIOLATION) is not None:
            # A valid token for a user that no longer exists.
            raise NotFoundError("User not found.", code="USER_NOT_FOUND") from None
        raise
    return WalletResponse.model_validate(row)


async def get_balance(
    conn: AsyncConnection, wallet_id: int, *, caller: Principal
) -> BalanceResponse:
    """Balance, held and available from one row, so the three always agree."""
    async with transaction(conn):
        account = await repository.get_account(conn, wallet_id)

    # System accounts look like unknown wallets: users have no business with their ids.
    if account is None or account["account_type"] != "user_wallet":
        raise NotFoundError("Wallet not found.", code="WALLET_NOT_FOUND")
    if account["user_id"] != caller.id:
        raise ForbiddenError("You can only see your own wallets.")
    return BalanceResponse.model_validate({**account, "wallet_id": account["id"]})
