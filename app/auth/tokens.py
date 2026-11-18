"""JWT access tokens: HS256, claims `sub` (user id), `role`, `iat`, `exp`. No refresh tokens.

Expiry is checked against an injected `now`, not PyJWT's wall clock, so tests can pin time.
The role travels in the token: admin routes need no database read. A role change therefore
takes effect when the user's current token expires (at most the token lifetime).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast

import jwt

ALGORITHM = "HS256"
Role = Literal["user", "admin"]
_ROLES: tuple[Role, ...] = ("user", "admin")


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    id: int
    role: Role

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class InvalidTokenError(Exception):
    """The token is missing claims, malformed, wrongly signed, or expired."""


def issue_token(principal: Principal, *, secret: str, now: datetime, ttl: timedelta) -> str:
    claims = {
        "sub": str(principal.id),  # RFC 7519: sub is a string
        "role": principal.role,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def decode_token(token: str, *, secret: str, now: datetime) -> Principal:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],  # pinned: never trust the header's alg
            options={
                "require": ["sub", "role", "iat", "exp"],
                "verify_exp": False,  # checked below against the injected clock
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    exp, iat, sub, role = claims["exp"], claims["iat"], claims["sub"], claims["role"]
    if not isinstance(exp, int) or not isinstance(iat, int):
        raise InvalidTokenError("iat and exp must be integers")
    if now.timestamp() >= exp:
        raise InvalidTokenError("token has expired")
    if iat > now.timestamp():
        raise InvalidTokenError("token issued in the future")
    if not (isinstance(sub, str) and sub.isdigit() and sub[0] != "0"):
        raise InvalidTokenError("sub must be a positive integer id")
    if role not in _ROLES:
        raise InvalidTokenError("unknown role")
    return Principal(id=int(sub), role=cast(Role, role))
