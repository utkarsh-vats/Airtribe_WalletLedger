"""Injectable clock. Code that needs "now" takes a Clock, so tests can pin time instead of
reading the wall clock (DESIGN.md §11)."""

from datetime import UTC, datetime
from typing import Protocol

from fastapi import Request


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware (UTC)."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def get_clock(request: Request) -> Clock:
    """FastAPI dependency: the app's clock (SystemClock unless create_app was given one)."""
    return request.app.state.clock
