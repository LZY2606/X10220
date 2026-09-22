"""Deterministic virtual clock.

All timestamps in the system come from this clock so tests can run with
frozen, reproducible time and replays are stable.  The clock is driven by
the event sequence: event with sequence number ``seq`` gets timestamp
``BASE + seq seconds``.
"""
from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def ts_for_seq(seq):
    """Return the deterministic ISO-8601 timestamp for an event sequence."""
    return (BASE + timedelta(seconds=int(seq))).isoformat().replace("+00:00", "Z")
