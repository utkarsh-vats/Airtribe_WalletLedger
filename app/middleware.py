"""Request ID middleware.

Every request gets an ID: the client's `X-Request-ID` if it is sane, otherwise a fresh uuid7.
It is stored on `request.state.request_id` (for error bodies), in a context variable (for logs),
and echoed in the `X-Request-ID` response header.
"""

import re
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"

# Client-supplied IDs end up in logs and response headers: keep them short and boring.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """The current request's ID, or None outside a request."""
    return _request_id.get()


def _pick_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            if _VALID_REQUEST_ID.fullmatch(candidate):
                return candidate
            break
    return str(uuid.uuid7())


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _pick_request_id(scope)
        # scope["state"] backs request.state; Starlette's outer error middleware sees the same
        # dict, so the 500 handler can read the ID too.
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id.set(request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _request_id.reset(token)
