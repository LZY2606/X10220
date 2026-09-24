"""时钟抽象。默认真实 UTC；测试/案例使用固定的虚拟时间。"""
from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    def now_iso(self) -> str:
        raise NotImplementedError


class SystemClock(Clock):
    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class VirtualClock(Clock):
    """从给定起点按固定步长推进，保证重放时间戳确定。"""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00",
                 step_seconds: float = 1.0):
        self._t = datetime.fromisoformat(start)
        self._step = step_seconds

    def now_iso(self) -> str:
        from datetime import timedelta
        ts = self._t.isoformat(timespec="microseconds")
        self._t = self._t + timedelta(seconds=self._step)
        return ts
