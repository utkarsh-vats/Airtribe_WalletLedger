"""Application errors and the single error shape: {"code", "message", "request_id"}.

Services raise `AppError` subclasses; handlers here turn them (and framework errors) into
JSON. Request-validation failures map to 400 so that 422 stays reserved for semantic
rejections (INSUFFICIENT_FUNDS, IDEMPOTENCY_KEY_REUSED, ...).
"""

import logging
from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.middleware import REQUEST_ID_HEADER

logger = logging.getLogger("app.errors")


class AppError(Exception):
    """Base for every error the API returns on purpose."""

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "INTERNAL_ERROR"
    default_message: ClassVar[str] = "Internal server error."

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.message = message or self.default_message
        if code is not None:
            self.code = code  # type: ignore[misc]  # per-instance override of the class default
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    code = "BAD_REQUEST"
    default_message = "Bad request."


class UnauthorizedError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"
    default_message = "Authentication required."


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"
    default_message = "You are not allowed to do this."


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    default_message = "Not found."


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"
    default_message = "Conflict."


class UnprocessableError(AppError):
    status_code = 422
    code = "UNPROCESSABLE"
    default_message = "The request can't be processed."


_HTTP_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error_response(
    request: Request, status_code: int, code: str, message: str, *, add_header: bool = False
) -> JSONResponse:
    request_id = _request_id(request)
    headers = {REQUEST_ID_HEADER: request_id} if add_header and request_id else None
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message, "request_id": request_id},
        headers=headers,
    )


def _format_validation_errors(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else err.get("msg", "invalid"))
    return "; ".join(parts) or "Invalid request."


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(request, exc.status_code, exc.code, exc.message)


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(request, 400, "VALIDATION_ERROR", _format_validation_errors(exc))


async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_STATUS_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
    return error_response(request, exc.status_code, code, str(exc.detail))


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Runs in Starlette's outermost middleware, outside RequestIdMiddleware's send wrapper,
    # so the header is added here. Never leak exception details to the client.
    logger.exception("unhandled error request_id=%s", _request_id(request), exc_info=exc)
    return error_response(request, 500, AppError.code, AppError.default_message, add_header=True)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_error_handler)
