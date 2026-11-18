"""Step 2.1c: POST /auth/login against the real test database."""

import uuid
from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth import service
from app.auth.tokens import Principal, decode_token
from app.config import get_settings
from tests.fakes import FixedClock
from tests.integration.conftest import API_TEST_EMAIL_DOMAIN

PASSWORD = "correct horse battery"


def _email(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}@{API_TEST_EMAIL_DOMAIN}"


def _phone() -> str:
    return f"+9197{uuid.uuid4().int % 10**8:08d}"


async def _register(api: AsyncClient, email: str) -> int:
    response = await api.post(
        "/users",
        json={"name": "Ravi", "email": email, "phone": _phone(), "password": PASSWORD},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _decode(token: str, clock: FixedClock) -> Principal:
    return decode_token(
        token, secret=get_settings().jwt_secret.get_secret_value(), now=clock.current
    )


async def test_login_returns_a_bearer_token_for_the_user(
    api: AsyncClient, clock: FixedClock
) -> None:
    email = _email("ravi")
    user_id = await _register(api, email)

    response = await api.post("/auth/login", json={"email": email, "password": PASSWORD})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type"}
    assert body["token_type"] == "bearer"
    assert _decode(body["access_token"], clock) == Principal(id=user_id, role="user")


async def test_token_expires_after_sixty_minutes(api: AsyncClient, clock: FixedClock) -> None:
    email = _email("ttl")
    await _register(api, email)
    token = (await api.post("/auth/login", json={"email": email, "password": PASSWORD})).json()[
        "access_token"
    ]
    issued = clock.current

    clock.current = issued + timedelta(minutes=60) - timedelta(seconds=1)
    _decode(token, clock)
    clock.current = issued + timedelta(minutes=60)
    with pytest.raises(Exception, match="expired"):
        _decode(token, clock)


async def test_email_match_ignores_case(api: AsyncClient) -> None:
    email = _email("case")
    await _register(api, email)

    response = await api.post("/auth/login", json={"email": email.upper(), "password": PASSWORD})

    assert response.status_code == 200


async def test_wrong_password_and_unknown_email_look_the_same(api: AsyncClient) -> None:
    email = _email("same")
    await _register(api, email)

    wrong_password = await api.post("/auth/login", json={"email": email, "password": "nope"})
    unknown_email = await api.post(
        "/auth/login", json={"email": _email("nobody"), "password": PASSWORD}
    )

    for response in (wrong_password, unknown_email):
        assert response.status_code == 401
        body = response.json()
        del body["request_id"]
        assert body == {"code": "INVALID_CREDENTIALS", "message": "Invalid email or password."}


async def test_unknown_email_still_spends_a_password_verify(
    api: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def spy(password: str) -> None:
        calls.append(password)

    monkeypatch.setattr(service, "verify_against_dummy", spy)

    response = await api.post("/auth/login", json={"email": _email("ghost"), "password": "guess"})

    assert response.status_code == 401
    assert calls == ["guess"]


async def test_outdated_hash_is_upgraded_on_login(
    api: AsyncClient, owner_engine: AsyncEngine
) -> None:
    email = _email("legacy")
    old_hash = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4).hash(PASSWORD)
    async with owner_engine.begin() as owner:
        user_id = await owner.scalar(
            text(
                "INSERT INTO users (name, email, phone, password_hash) "
                "VALUES ('Legacy', :e, :p, :h) RETURNING id"
            ),
            {"e": email, "p": _phone(), "h": old_hash},
        )

    first = await api.post("/auth/login", json={"email": email, "password": PASSWORD})

    async with owner_engine.connect() as owner:
        stored = await owner.scalar(
            text("SELECT password_hash FROM users WHERE id = :i"), {"i": user_id}
        )
    assert first.status_code == 200
    assert stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    second = await api.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert second.status_code == 200  # the upgraded hash still verifies


async def test_wrong_password_does_not_upgrade_the_hash(
    api: AsyncClient, owner_engine: AsyncEngine
) -> None:
    email = _email("legacy-wrong")
    old_hash = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4).hash(PASSWORD)
    async with owner_engine.begin() as owner:
        user_id = await owner.scalar(
            text(
                "INSERT INTO users (name, email, phone, password_hash) "
                "VALUES ('Legacy', :e, :p, :h) RETURNING id"
            ),
            {"e": email, "p": _phone(), "h": old_hash},
        )

    response = await api.post("/auth/login", json={"email": email, "password": "wrong"})

    async with owner_engine.connect() as owner:
        stored = await owner.scalar(
            text("SELECT password_hash FROM users WHERE id = :i"), {"i": user_id}
        )
    assert response.status_code == 401
    assert stored == old_hash


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "a@example.com"},
        {"email": "a@example.com", "password": ""},
        {"email": "a@example.com", "password": PASSWORD, "role": "admin"},
    ],
    ids=["bad-email", "missing-password", "empty-password", "extra-field"],
)
async def test_invalid_body_is_400(api: AsyncClient, body: dict[str, object]) -> None:
    response = await api.post("/auth/login", json=body)

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
