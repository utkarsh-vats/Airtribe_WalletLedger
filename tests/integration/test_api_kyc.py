"""Step 2.1d: POST /admin/users/{id}/kyc."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.passwords import hash_password
from tests.integration.conftest import API_TEST_EMAIL_DOMAIN

PASSWORD = "correct horse battery"


def _email(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}@{API_TEST_EMAIL_DOMAIN}"


def _phone() -> str:
    return f"+9196{uuid.uuid4().int % 10**8:08d}"


async def _token(api: AsyncClient, email: str) -> dict[str, str]:
    response = await api.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def admin_headers(api: AsyncClient, owner_engine: AsyncEngine) -> dict[str, str]:
    email = _email("admin")
    async with owner_engine.begin() as owner:  # admins exist only via the seed (as owner)
        await owner.execute(
            text(
                "INSERT INTO users (name, email, phone, password_hash, role) "
                "VALUES ('Admin', :e, :p, :h, 'admin')"
            ),
            {"e": email, "p": _phone(), "h": await hash_password(PASSWORD)},
        )
    return await _token(api, email)


@pytest.fixture
async def user(api: AsyncClient) -> tuple[int, dict[str, str]]:
    email = _email("kyc")
    response = await api.post(
        "/users", json={"name": "Meera", "email": email, "phone": _phone(), "password": PASSWORD}
    )
    return response.json()["id"], await _token(api, email)


@pytest.mark.parametrize("kyc_status", ["verified", "rejected", "pending"])
async def test_admin_sets_kyc_status(
    api: AsyncClient,
    admin_headers: dict[str, str],
    user: tuple[int, dict[str, str]],
    owner_engine: AsyncEngine,
    kyc_status: str,
) -> None:
    user_id, _ = user

    response = await api.post(
        f"/admin/users/{user_id}/kyc", json={"kyc_status": kyc_status}, headers=admin_headers
    )

    assert response.status_code == 200
    assert response.json()["id"] == user_id
    assert response.json()["kyc_status"] == kyc_status
    async with owner_engine.connect() as owner:
        stored = await owner.scalar(
            text("SELECT kyc_status FROM users WHERE id = :i"), {"i": user_id}
        )
    assert stored == kyc_status


async def test_non_admin_is_403(api: AsyncClient, user: tuple[int, dict[str, str]]) -> None:
    user_id, user_headers = user

    response = await api.post(
        f"/admin/users/{user_id}/kyc", json={"kyc_status": "verified"}, headers=user_headers
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


async def test_no_token_is_401(api: AsyncClient, user: tuple[int, dict[str, str]]) -> None:
    user_id, _ = user

    response = await api.post(f"/admin/users/{user_id}/kyc", json={"kyc_status": "verified"})

    assert response.status_code == 401


async def test_unknown_user_is_404(api: AsyncClient, admin_headers: dict[str, str]) -> None:
    response = await api.post(
        "/admin/users/999999999/kyc", json={"kyc_status": "verified"}, headers=admin_headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == "USER_NOT_FOUND"


@pytest.mark.parametrize(
    ("path_id", "body"),
    [
        ("1", {"kyc_status": "approved"}),
        ("1", {}),
        ("1", {"kyc_status": "verified", "role": "admin"}),
        ("0", {"kyc_status": "verified"}),
        ("abc", {"kyc_status": "verified"}),
    ],
    ids=["unknown-status", "missing", "extra-field", "zero-id", "non-numeric-id"],
)
async def test_invalid_request_is_400(
    api: AsyncClient, admin_headers: dict[str, str], path_id: str, body: dict[str, object]
) -> None:
    response = await api.post(f"/admin/users/{path_id}/kyc", json=body, headers=admin_headers)

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
