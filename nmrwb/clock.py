from __future__ import annotations

from datetime import datetime, timedelta, timezone


class VirtualClock:
    """Deterministic clock: every now() advances by a fixed step.

    Tests and seeding share this so all timestamps are reproducible
    and never depend on the wall clock or the network.
    """

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00", step_seconds: float = 1.0):
        self._t = datetime.fromisoformat(start)
        if self._t.tzinfo is None:
            self._t = self._t.replace(tzinfo=timezone.utc)
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> str:
        value = self._t
        self._t = self._t + self._step
        return value.isoformat().replace("+00:00", "Z")
