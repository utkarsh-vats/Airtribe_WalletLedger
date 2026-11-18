"""FastAPI dependencies: `current_user` (any valid token) and `require_admin`."""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.tokens import InvalidTokenError, Principal, decode_token
from app.clock import Clock, get_clock
from app.config import Settings
from app.errors import ForbiddenError, UnauthorizedError

# auto_error=False: we raise our own 401 in the standard error shape. Still registers the
# Bearer scheme in OpenAPI, so /docs gets an "Authorize" button.
_bearer = HTTPBearer(auto_error=False)

_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> Principal:
    if credentials is None:
        raise UnauthorizedError("Missing bearer token.", headers=_CHALLENGE)
    try:
        return decode_token(
            credentials.credentials,
            secret=settings.jwt_secret.get_secret_value(),
            now=clock.now(),
        )
    except InvalidTokenError:
        # One message for every failure: don't tell a caller which check its token failed.
        raise UnauthorizedError("Invalid or expired token.", headers=_CHALLENGE) from None


async def require_admin(user: Annotated[Principal, Depends(current_user)]) -> Principal:
    if not user.is_admin:
        raise ForbiddenError("Admin access required.")
    return user


CurrentUser = Annotated[Principal, Depends(current_user)]
AdminUser = Annotated[Principal, Depends(require_admin)]
