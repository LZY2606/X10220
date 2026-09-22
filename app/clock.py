"""Deterministic virtual clock.

Tests freeze time and advance it explicitly. The demo runs against a fixed
epoch so replaying the bundled case always yields identical timestamps and
event ids.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional


class VirtualClock:
    def __init__(self, start_iso: Optional[str] = None, frozen: bool = True):
        self.frozen = frozen
        if start_iso is not None:
            self._now = _dt.datetime.fromisoformat(start_iso).replace(tzinfo=_dt.timezone.utc)
        else:
            self._now = _dt.datetime(2026, 9, 1, 9, 0, 0, tzinfo=_dt.timezone.utc)

    def now_iso(self) -> str:
        return self._now.isoformat()

    def advance(self, seconds: int = 60) -> str:
        self._now += _dt.timedelta(seconds=seconds)
        return self.now_iso()

    def set(self, iso: str) -> str:
        self._now = _dt.datetime.fromisoformat(iso).replace(tzinfo=_dt.timezone.utc)
        return self.now_iso()
