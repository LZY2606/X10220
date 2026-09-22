"""Deterministic virtual clock used by projects and tests.

No wall clock is ever consulted for state.  Time starts at a fixed epoch
and advances one tick per mutation; the clock lives in the database so a
reopened project continues exactly where it stopped.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

EPOCH_ISO = "2026-01-01T00:00:00+00:00"
TICK_SECONDS = 7


def epoch() -> datetime:
    return datetime.fromisoformat(EPOCH_ISO)


def iso_at(tick: int) -> str:
    return (epoch() + timedelta(seconds=tick * TICK_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


def tick_to_dt(tick: int) -> datetime:
    return epoch() + timedelta(seconds=tick * TICK_SECONDS)
