"""Virtual-time clock. All timestamps in the workbench come from an
injectable clock so tests can run on fixed virtual time."""
from datetime import datetime, timedelta, timezone


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


class FixedClock:
    """Deterministic clock for tests: starts at `start`, advances `step` per call."""

    def __init__(self, start="2026-01-01T00:00:00+00:00", step_seconds=1):
        self.current = datetime.fromisoformat(start)
        self.step = timedelta(seconds=step_seconds)

    def __call__(self):
        value = self.current.isoformat()
        self.current = self.current + self.step
        return value
