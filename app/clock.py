"""Injectable clock. Tests freeze virtual time; production uses UTC now."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, List, Optional


class Clock:
    def __init__(self, fn: Optional[Callable[[], datetime]] = None) -> None:
        self._fn = fn or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        value = self._fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def now_iso(self) -> str:
        return self.now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def virtual_clock(start: str = "2026-09-01T09:00:00Z", step_seconds: int = 1) -> Clock:
    """Clock that advances a fixed amount on every read (deterministic tests)."""
    ticks: List[datetime] = []

    def fn() -> datetime:
        base = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        ticks.append(base)
        return base + _timedelta(step_seconds * (len(ticks) - 1))

    return Clock(fn)


def frozen_clock(start: str = "2026-09-01T09:00:00Z") -> Clock:
    base = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return Clock(lambda: base)


def _timedelta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)
