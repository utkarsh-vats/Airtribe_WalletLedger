"""Test doubles shared by unit and integration tests."""

from datetime import datetime


class FixedClock:
    """A Clock that returns a set time; tests move it by assigning `current`."""

    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current
