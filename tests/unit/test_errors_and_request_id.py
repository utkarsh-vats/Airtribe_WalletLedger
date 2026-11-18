import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.errors import ConflictError, UnprocessableError
from app.main import create_app


class Body(BaseModel):
    amount: int


def _app_with_test_routes() -> FastAPI:
    app = create_app()

    @app.post("/_test/validate")
    async def validate(body: Body) -> dict[str, int]:
        return {"amount": body.amount}

    @app.get("/_test/insufficient")
    async def insufficient() -> None:
        raise UnprocessableError("Not enough money.", code="INSUFFICIENT_FUNDS")

    @app.get("/_test/conflict")
    async def conflict() -> None:
        raise ConflictError()

    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=False: Starlette re-raises after sending the 500; we want the response.
    transport = ASGITransport(app=_app_with_test_routes(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _assert_error_shape(body: dict, code: str, request_id: str) -> None:
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == code
    assert body["request_id"] == request_id
    assert isinstance(body["message"], str) and body["message"]


# --- request id ---


async def test_generates_uuid7_request_id_when_absent(client: AsyncClient) -> None:
    response = await client.get("/health")

    request_id = response.headers["X-Request-ID"]
    assert uuid.UUID(request_id).version == 7


async def test_echoes_valid_client_request_id(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "client-abc_123.4"})

    assert response.headers["X-Request-ID"] == "client-abc_123.4"


@pytest.mark.parametrize("bad", ["has space", "x" * 129, "semi;colon", ""])
async def test_replaces_unsafe_client_request_id(client: AsyncClient, bad: str) -> None:
    response = await client.get("/health", headers={"X-Request-ID": bad})

    assert response.headers["X-Request-ID"] != bad
    assert uuid.UUID(response.headers["X-Request-ID"]).version == 7


async def test_each_request_gets_its_own_id(client: AsyncClient) -> None:
    first = await client.get("/health")
    second = await client.get("/health")

    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


# --- error shape ---


async def test_app_error_uses_its_status_and_code(client: AsyncClient) -> None:
    response = await client.get("/_test/insufficient", headers={"X-Request-ID": "r-1"})

    assert response.status_code == 422
    _assert_error_shape(response.json(), "INSUFFICIENT_FUNDS", "r-1")
    assert response.json()["message"] == "Not enough money."
    assert response.headers["X-Request-ID"] == "r-1"


async def test_app_error_default_code_and_message(client: AsyncClient) -> None:
    response = await client.get("/_test/conflict", headers={"X-Request-ID": "r-2"})

    assert response.status_code == 409
    _assert_error_shape(response.json(), "CONFLICT", "r-2")


async def test_request_validation_maps_to_400(client: AsyncClient) -> None:
    response = await client.post(
        "/_test/validate", json={"amount": "lots"}, headers={"X-Request-ID": "r-3"}
    )

    assert response.status_code == 400
    _assert_error_shape(response.json(), "VALIDATION_ERROR", "r-3")
    assert "amount" in response.json()["message"]


async def test_unknown_route_is_404_in_error_shape(client: AsyncClient) -> None:
    response = await client.get("/nope", headers={"X-Request-ID": "r-4"})

    assert response.status_code == 404
    _assert_error_shape(response.json(), "NOT_FOUND", "r-4")


async def test_wrong_method_is_405_in_error_shape(client: AsyncClient) -> None:
    response = await client.post("/health", headers={"X-Request-ID": "r-5"})

    assert response.status_code == 405
    _assert_error_shape(response.json(), "METHOD_NOT_ALLOWED", "r-5")


async def test_unhandled_error_is_500_without_leaking_details(client: AsyncClient) -> None:
    response = await client.get("/_test/boom", headers={"X-Request-ID": "r-6"})

    assert response.status_code == 500
    _assert_error_shape(response.json(), "INTERNAL_ERROR", "r-6")
    assert "secret" not in response.text
    assert response.headers["X-Request-ID"] == "r-6"
