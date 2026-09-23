"""时钟抽象:测试使用虚拟时间,服务默认系统时间(UTC)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class VirtualClock:
    """固定起点、固定步长的确定性时钟。"""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00", step_seconds: int = 1):
        self._t = datetime.fromisoformat(start)
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> str:
        t = self._t
        self._t = self._t + self._step
        return t.isoformat()
