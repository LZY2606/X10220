"""Clock injection: production uses wall time, tests use a virtual counter."""
from __future__ import annotations

import itertools
import time
from typing import Callable


class VirtualClock:
    def __init__(self, start: str = "2026-09-28T09:00:00+08:00"):
        self._n = itertools.count()
        self._start = start

    def now(self) -> str:
        return f"{self._start}#v{next(self._n):04d}"


def wall_clock() -> Callable[[], str]:
    return lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z")
