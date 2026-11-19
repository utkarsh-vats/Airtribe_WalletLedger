"""Step 2.2: POST /users/{id}/wallets and GET /wallets/{id}/balance."""

import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import API_TEST_EMAIL_DOMAIN

PASSWORD = "correct horse battery"


@dataclass
class Caller:
    id: int
    headers: dict[str, str]


async def _caller(api: AsyncClient, tag: str) -> Caller:
    email = f"{tag}-{uuid.uuid4().hex[:8]}@{API_TEST_EMAIL_DOMAIN}"
    phone = f"+9195{uuid.uuid4().int % 10**8:08d}"
    created = await api.post(
        "/users", json={"name": tag, "email": email, "phone": phone, "password": PASSWORD}
    )
    login = await api.post("/auth/login", json={"email": email, "password": PASSWORD})
    return Caller(created.json()["id"], {"Authorization": f"Bearer {login.json()['access_token']}"})


@pytest.fixture
async def alice(api: AsyncClient) -> Caller:
    return await _caller(api, "alice")


@pytest.fixture
async def bob(api: AsyncClient) -> Caller:
    return await _caller(api, "bob")


# --- POST /users/{id}/wallets ---


async def test_open_wallet_starts_empty_in_inr(api: AsyncClient, alice: Caller) -> None:
    response = await api.post(
        f"/users/{alice.id}/wallets", json={"label": " savings "}, headers=alice.headers
    )

    assert response.status_code == 201
    wallet = response.json()
    assert set(wallet) == {
        "id",
        "user_id",
        "label",
        "currency",
        "balance",
        "held_amount",
        "available",
        "created_at",
    }
    assert wallet["user_id"] == alice.id
    assert wallet["label"] == "savings"
    assert (wallet["currency"], wallet["balance"], wallet["held_amount"], wallet["available"]) == (
        "INR",
        0,
        0,
        0,
    )


async def test_label_defaults_to_main(api: AsyncClient, alice: Caller) -> None:
    without_body = await api.post(f"/users/{alice.id}/wallets", headers=alice.headers)

    assert without_body.status_code == 201
    assert without_body.json()["label"] == "main"


async def test_same_label_twice_is_409(api: AsyncClient, alice: Caller) -> None:
    await api.post(f"/users/{alice.id}/wallets", json={"label": "main"}, headers=alice.headers)

    again = await api.post(
        f"/users/{alice.id}/wallets", json={"label": "main"}, headers=alice.headers
    )

    assert again.status_code == 409
    assert again.json()["code"] == "WALLET_LABEL_TAKEN"


async def test_same_label_for_another_user_is_fine(
    api: AsyncClient, alice: Caller, bob: Caller
) -> None:
    a = await api.post(f"/users/{alice.id}/wallets", json={"label": "main"}, headers=alice.headers)
    b = await api.post(f"/users/{bob.id}/wallets", json={"label": "main"}, headers=bob.headers)

    assert (a.status_code, b.status_code) == (201, 201)


async def test_cannot_open_a_wallet_for_someone_else(
    api: AsyncClient, alice: Caller, bob: Caller, owner_engine: AsyncEngine
) -> None:
    response = await api.post(f"/users/{bob.id}/wallets", headers=alice.headers)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    async with owner_engine.connect() as owner:
        count = await owner.scalar(
            text("SELECT count(*) FROM accounts WHERE user_id = :u"), {"u": bob.id}
        )
    assert count == 0


async def test_open_wallet_without_token_is_401(api: AsyncClient, alice: Caller) -> None:
    response = await api.post(f"/users/{alice.id}/wallets")

    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [{"label": ""}, {"label": "   "}, {"label": "x" * 51}, {"label": "a", "balance": 100}],
    ids=["empty", "blank", "too-long", "extra-field"],
)
async def test_invalid_wallet_body_is_400(
    api: AsyncClient, alice: Caller, body: dict[str, object]
) -> None:
    response = await api.post(f"/users/{alice.id}/wallets", json=body, headers=alice.headers)

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


# --- GET /wallets/{id}/balance ---


async def _wallet(api: AsyncClient, caller: Caller, label: str = "main") -> int:
    response = await api.post(
        f"/users/{caller.id}/wallets", json={"label": label}, headers=caller.headers
    )
    return response.json()["id"]


async def test_balance_of_a_new_wallet(api: AsyncClient, alice: Caller) -> None:
    wallet_id = await _wallet(api, alice)

    response = await api.get(f"/wallets/{wallet_id}/balance", headers=alice.headers)

    assert response.status_code == 200
    assert response.json() == {
        "wallet_id": wallet_id,
        "currency": "INR",
        "balance": 0,
        "held_amount": 0,
        "available": 0,
    }


async def test_available_is_balance_minus_held(
    api: AsyncClient, alice: Caller, owner_engine: AsyncEngine
) -> None:
    wallet_id = await _wallet(api, alice)
    async with owner_engine.begin() as owner:  # no postings yet: set the numbers directly
        await owner.execute(
            text("UPDATE accounts SET balance = 150000, held_amount = 25000 WHERE id = :w"),
            {"w": wallet_id},
        )

    response = await api.get(f"/wallets/{wallet_id}/balance", headers=alice.headers)

    body = response.json()
    assert (body["balance"], body["held_amount"], body["available"]) == (150000, 25000, 125000)


async def test_someone_elses_wallet_is_403(api: AsyncClient, alice: Caller, bob: Caller) -> None:
    bobs_wallet = await _wallet(api, bob)

    response = await api.get(f"/wallets/{bobs_wallet}/balance", headers=alice.headers)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    "wallet_id", [1, 2, 3, 999_999_999], ids=["cash_in", "cash_out", "fee_revenue", "unknown"]
)
async def test_system_accounts_and_unknown_ids_are_404(
    api: AsyncClient, alice: Caller, wallet_id: int
) -> None:
    response = await api.get(f"/wallets/{wallet_id}/balance", headers=alice.headers)

    assert response.status_code == 404
    assert response.json()["code"] == "WALLET_NOT_FOUND"


async def test_balance_without_token_is_401(api: AsyncClient, alice: Caller) -> None:
    wallet_id = await _wallet(api, alice)

    response = await api.get(f"/wallets/{wallet_id}/balance")

    assert response.status_code == 401


@pytest.mark.parametrize("wallet_id", ["0", "-1", "abc"])
async def test_bad_wallet_id_is_400(api: AsyncClient, alice: Caller, wallet_id: str) -> None:
    response = await api.get(f"/wallets/{wallet_id}/balance", headers=alice.headers)

    assert response.status_code == 400
