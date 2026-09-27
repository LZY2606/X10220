from __future__ import annotations

from datetime import datetime, timedelta, timezone


class VirtualClock:
    """Deterministic monotonic clock. Every call to now() advances one step."""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00", step_seconds: int = 1):
        moment = datetime.fromisoformat(start)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._moment = moment
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> str:
        self._moment += self._step
        return self._moment.isoformat()


_clock = VirtualClock()


def set_clock(clock: VirtualClock) -> None:
    global _clock
    _clock = clock


def now_iso() -> str:
    return _clock.now()
