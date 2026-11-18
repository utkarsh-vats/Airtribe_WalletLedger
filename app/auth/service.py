"""Login (DESIGN.md §6). Owns its transactions.

The slow parts (argon2 verify and rehash) run between short transactions, never inside one,
so a login never holds a connection's transaction open while hashing.
"""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth import repository
from app.auth.passwords import hash_password, needs_rehash, verify_against_dummy, verify_password
from app.auth.schemas import LoginRequest, TokenResponse
from app.auth.tokens import Principal, issue_token
from app.clock import Clock
from app.config import Settings
from app.db import transaction
from app.errors import UnauthorizedError

# Same error for unknown email and wrong password: login must not reveal which emails exist.
_INVALID = ("Invalid email or password.", "INVALID_CREDENTIALS")


async def login(
    conn: AsyncConnection, request: LoginRequest, *, settings: Settings, clock: Clock
) -> TokenResponse:
    async with transaction(conn):
        user = await repository.find_login_user(conn, request.email)

    if user is None:
        await verify_against_dummy(request.password)  # same cost as a real verify
        raise UnauthorizedError(_INVALID[0], code=_INVALID[1])
    if not await verify_password(user.password_hash, request.password):
        raise UnauthorizedError(_INVALID[0], code=_INVALID[1])

    if needs_rehash(user.password_hash):
        new_hash = await hash_password(request.password)
        async with transaction(conn):
            # If the hash changed meanwhile (e.g. a concurrent login already upgraded it),
            # leave the newer one alone.
            await repository.replace_password_hash(
                conn, user_id=user.id, old_hash=user.password_hash, new_hash=new_hash
            )

    token = issue_token(
        Principal(id=user.id, role=user.role),
        secret=settings.jwt_secret.get_secret_value(),
        now=clock.now(),
        ttl=timedelta(minutes=settings.jwt_ttl_minutes),
    )
    return TokenResponse(access_token=token)
