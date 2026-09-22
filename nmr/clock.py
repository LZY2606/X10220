"""Virtual clock: every timestamp in the system comes from here so tests can
freeze time and replays stay deterministic."""

from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self, start: str = "2026-01-01T00:00:00+00:00"):
        self._t = datetime.fromisoformat(start)
        if self._t.tzinfo is None:
            self._t = self._t.replace(tzinfo=timezone.utc)

    def now(self) -> str:
        self._t += timedelta(seconds=1)
        return self._t.isoformat()

    def peek(self) -> str:
        return self._t.isoformat()
