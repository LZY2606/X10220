"""Virtual clock.

Tests may freeze/advance time via :func:`set_clock`.  Production code only
reads time through :func:`now`, so "virtual time" is guaranteed to be used by
every layer (events, fingerprints, ordering).
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional


class _Clock:
    def __init__(self) -> None:
        self._fixed: Optional[_dt.datetime] = None

    def set(self, when: Optional[_dt.datetime]) -> None:
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        self._fixed = when

    def now(self) -> _dt.datetime:
        if self._fixed is not None:
            return self._fixed
        return _dt.datetime.now(_dt.timezone.utc)


_clock = _Clock()


def set_clock(when: Optional[_dt.datetime]) -> None:
    """Freeze time at ``when`` (naive values assumed UTC) or resume real time."""
    _clock.set(when)


def advance(seconds: float) -> None:
    cur = _clock.now()
    _clock.set(cur + _dt.timedelta(seconds=seconds))


def now() -> str:
    """ISO-8601 UTC timestamp used for every recorded event."""
    return _clock.now().isoformat()
