"""Step 2.1a: password hashing, JWT issue/verify, and the auth dependencies. No database.

Time is pinned with a fixed clock; nothing here reads the wall clock.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from app.auth.deps import AdminUser, CurrentUser
from app.auth.passwords import (
    hash_password,
    needs_rehash,
    verify_against_dummy,
    verify_password,
)
from app.auth.tokens import InvalidTokenError, Principal, decode_token, issue_token
from app.config import DEV_JWT_SECRET, Settings
from app.main import create_app

SECRET = "unit-test-secret-that-is-at-least-32-bytes-long"
T0 = datetime(2030, 1, 15, 10, 0, 0, tzinfo=UTC)
TTL = timedelta(minutes=60)
USER = Principal(id=7, role="user")
ADMIN = Principal(id=1, role="admin")


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


# --- passwords ---


def test_hash_is_argon2id_and_salted() -> None:
    first, second = hash_password("correct horse"), hash_password("correct horse")

    assert first.startswith("$argon2id$")
    assert first != second  # random salt
    assert "correct horse" not in first


def test_verify_password() -> None:
    stored = hash_password("correct horse")

    assert verify_password(stored, "correct horse") is True
    assert verify_password(stored, "Correct horse") is False
    assert verify_password("not-a-hash", "correct horse") is False
    assert needs_rehash(stored) is False


def test_verify_against_dummy_never_raises() -> None:
    verify_against_dummy("anything")


# --- tokens ---


def _raw(claims: dict[str, object], secret: str = SECRET) -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


def _claims(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sub": "7",
        "role": "user",
        "iat": int(T0.timestamp()),
        "exp": int((T0 + TTL).timestamp()),
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_round_trip() -> None:
    token = issue_token(ADMIN, secret=SECRET, now=T0, ttl=TTL)

    assert decode_token(token, secret=SECRET, now=T0) == ADMIN
    assert jwt.get_unverified_header(token)["alg"] == "HS256"


def test_valid_until_one_second_before_expiry_then_expired() -> None:
    token = issue_token(USER, secret=SECRET, now=T0, ttl=TTL)

    assert decode_token(token, secret=SECRET, now=T0 + TTL - timedelta(seconds=1)) == USER
    with pytest.raises(InvalidTokenError, match="expired"):
        decode_token(token, secret=SECRET, now=T0 + TTL)


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (_raw(_claims(), secret="another-secret-that-is-at-least-32-bytes"), "wrong secret"),
        (_raw(_claims(role=None)), "missing role"),
        (_raw(_claims(exp=None)), "missing exp"),
        (_raw(_claims(role="root")), "unknown role"),
        (_raw(_claims(sub="abc")), "non-numeric sub"),
        (_raw(_claims(sub="0")), "zero id"),
        (_raw(_claims(sub="-1")), "negative id"),
        (_raw(_claims(iat=int((T0 + timedelta(minutes=5)).timestamp()))), "issued in future"),
        (jwt.encode(_claims(), key=None, algorithm="none"), "alg none"),  # type: ignore[arg-type]
        ("not.a.jwt", "garbage"),
    ],
    ids=lambda v: v if isinstance(v, str) and " " in v else None,
)
def test_rejected_tokens(token: str, reason: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_token(token, secret=SECRET, now=T0)


def test_tampered_payload_is_rejected() -> None:
    header, _, signature = issue_token(USER, secret=SECRET, now=T0, ttl=TTL).split(".")
    forged_payload = _raw(_claims(role="admin")).split(".")[1]

    with pytest.raises(InvalidTokenError):
        decode_token(f"{header}.{forged_payload}.{signature}", secret=SECRET, now=T0)


# --- settings ---


def test_short_jwt_secret_refused() -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(jwt_secret=SecretStr("too-short"))


def test_dev_jwt_secret_refused_in_prod() -> None:
    with pytest.raises(ValidationError, match="APP_ENV=prod"):
        Settings(app_env="prod", jwt_secret=SecretStr(DEV_JWT_SECRET))

    assert Settings(app_env="prod", jwt_secret=SecretStr(SECRET)).app_env == "prod"


# --- dependencies ---


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
async def client(clock: FixedClock) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(Settings(jwt_secret=SecretStr(SECRET)), clock=clock)

    @app.get("/_test/me")
    async def me(user: CurrentUser) -> dict[str, object]:
        return {"id": user.id, "role": user.role}

    @app.get("/_test/admin")
    async def admin_only(user: AdminUser) -> dict[str, object]:
        return {"id": user.id}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _bearer(principal: Principal, now: datetime = T0) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(principal, secret=SECRET, now=now, ttl=TTL)}"}


async def test_current_user_from_token(client: AsyncClient) -> None:
    response = await client.get("/_test/me", headers=_bearer(USER))

    assert response.status_code == 200
    assert response.json() == {"id": 7, "role": "user"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer not.a.jwt"},
        {"Authorization": "Bearer"},
    ],
    ids=["missing", "wrong-scheme", "garbage", "empty"],
)
async def test_unauthenticated_requests_get_401(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/_test/me", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    body = response.json()
    assert body["code"] == "UNAUTHORIZED"
    assert body["request_id"] == response.headers["X-Request-ID"]


async def test_expired_token_gets_401(client: AsyncClient, clock: FixedClock) -> None:
    headers = _bearer(USER)
    clock.current = T0 + TTL

    response = await client.get("/_test/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["message"] == "Invalid or expired token."


async def test_admin_route_forbids_users_and_allows_admins(client: AsyncClient) -> None:
    as_user = await client.get("/_test/admin", headers=_bearer(USER))
    as_admin = await client.get("/_test/admin", headers=_bearer(ADMIN))

    assert as_user.status_code == 403
    assert as_user.json()["code"] == "FORBIDDEN"
    assert as_admin.status_code == 200
    assert as_admin.json() == {"id": 1}


async def test_admin_route_without_token_is_401_not_403(client: AsyncClient) -> None:
    response = await client.get("/_test/admin")

    assert response.status_code == 401
