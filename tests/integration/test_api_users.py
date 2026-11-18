"""Step 2.1b: POST /users against the real test database."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import API_TEST_EMAIL_DOMAIN


def _email(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}@{API_TEST_EMAIL_DOMAIN}"


def _phone() -> str:
    return f"+9198{uuid.uuid4().int % 10**8:08d}"  # unique enough within a run


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Asha Rao",
        "email": _email("asha"),
        "phone": _phone(),
        "password": "correct horse battery",
    }
    body.update(overrides)
    return body


async def test_create_user_returns_201_with_defaults(api: AsyncClient) -> None:
    body = _body(name="  Asha Rao  ")

    response = await api.post("/users", json=body)

    assert response.status_code == 201
    user = response.json()
    assert set(user) == {"id", "name", "email", "phone", "role", "kyc_status", "created_at"}
    assert user["name"] == "Asha Rao"
    assert (user["email"], user["phone"]) == (body["email"], body["phone"])
    assert (user["role"], user["kyc_status"]) == ("user", "pending")
    assert "password" not in response.text


async def test_password_is_stored_as_argon2id_hash(
    api: AsyncClient, owner_engine: AsyncEngine
) -> None:
    response = await api.post("/users", json=_body())

    async with owner_engine.connect() as owner:
        stored = await owner.scalar(
            text("SELECT password_hash FROM users WHERE id = :i"), {"i": response.json()["id"]}
        )
    assert stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$")


async def test_phone_is_stored_normalized(api: AsyncClient) -> None:
    digits = _phone()[3:]  # the 10-digit national number

    response = await api.post("/users", json=_body(phone=f"{digits[:5]} {digits[5:]}"))

    assert response.status_code == 201
    assert response.json()["phone"] == f"+91{digits}"


async def test_duplicate_email_differing_only_in_case_is_409(api: AsyncClient) -> None:
    email = _email("dup")
    first = await api.post("/users", json=_body(email=email))

    second = await api.post("/users", json=_body(email=email.upper()))

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "EMAIL_ALREADY_REGISTERED"


async def test_same_phone_in_another_format_is_409(api: AsyncClient) -> None:
    phone = _phone()
    first = await api.post("/users", json=_body(phone=phone))

    second = await api.post("/users", json=_body(phone=f"0{phone[3:]}"))  # trunk-prefix form

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "PHONE_ALREADY_REGISTERED"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"name": "   "}, "name"),
        ({"email": "not-an-email"}, "email"),
        ({"phone": "12345"}, "phone"),
        ({"password": "short"}, "password"),
        ({"password": "x" * 129}, "password"),
        ({"role": "admin"}, "role"),  # unknown fields are rejected, not ignored
        ({"kyc_status": "verified"}, "kyc_status"),
    ],
    ids=["blank-name", "bad-email", "bad-phone", "short-password", "long-password", "role", "kyc"],
)
async def test_invalid_body_is_400(
    api: AsyncClient, overrides: dict[str, object], field: str
) -> None:
    response = await api.post("/users", json=_body(**overrides))

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert field in body["message"]


async def test_missing_field_is_400(api: AsyncClient) -> None:
    body = _body()
    del body["phone"]

    response = await api.post("/users", json=body)

    assert response.status_code == 400
    assert "phone" in response.json()["message"]
