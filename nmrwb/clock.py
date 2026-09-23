from datetime import datetime, timezone, timedelta


class Clock:
    """Wall clock; production time source."""

    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class VirtualClock(Clock):
    """Deterministic clock for tests and replays: +1s per call."""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00"):
        self._t = datetime.fromisoformat(start)

    def now(self) -> str:
        t = self._t
        self._t += timedelta(seconds=1)
        return t.isoformat()
