"""Clocks. Tests use a deterministic virtual clock; the app uses wall time."""

from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    def now_ms(self) -> int:
        raise NotImplementedError


class SystemClock(Clock):
    def now_ms(self) -> int:
        return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


class FakeClock(Clock):
    """Deterministic monotonic virtual time in milliseconds."""

    def __init__(self, start_ms: int = 1_760_000_000_000, step_ms: int = 7):
        self._t = start_ms
        self._step = step_ms

    def now_ms(self) -> int:
        value = self._t
        self._t += self._step
        return value
