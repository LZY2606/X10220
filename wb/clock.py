"""Clocks. Tests use VirtualClock so no wall-clock or network is needed."""

from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class VirtualClock:
    """Deterministic clock: each call advances by a fixed step."""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00", step_seconds: int = 1):
        self._t = datetime.fromisoformat(start)
        if self._t.tzinfo is None:
            self._t = self._t.replace(tzinfo=timezone.utc)
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> str:
        t = self._t
        self._t = self._t + self._step
        return t.isoformat()
