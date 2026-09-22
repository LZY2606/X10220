from datetime import datetime, timedelta, timezone


class VirtualClock:
    """Deterministic clock: fixed start, fixed step per tick. No wall time."""

    def __init__(self, start="2026-01-01T00:00:00+00:00", step_seconds=1):
        self._t = datetime.fromisoformat(start)
        if self._t.tzinfo is None:
            self._t = self._t.replace(tzinfo=timezone.utc)
        self._step = timedelta(seconds=step_seconds)

    def now(self):
        value = self._t
        self._t = self._t + self._step
        return value.isoformat()
