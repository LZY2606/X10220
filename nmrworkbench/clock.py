"""Virtual-time clocks. Tests use FixedClock so no wall-clock dependence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    def now_iso(self) -> str:
        raise NotImplementedError


class SystemClock(Clock):
    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class FixedClock(Clock):
    """Deterministic clock: starts at a fixed instant, ticks by `step` per call."""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00", step_seconds: int = 1):
        self._t = datetime.fromisoformat(start)
        if self._t.tzinfo is None:
            self._t = self._t.replace(tzinfo=timezone.utc)
        self._step = timedelta(seconds=step_seconds)

    def now_iso(self) -> str:
        value = self._t.isoformat()
        self._t = self._t + self._step
        return value
