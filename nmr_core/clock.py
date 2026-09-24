"""可注入时钟：测试与案例重放使用虚拟时间，服务运行使用系统时间。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class VirtualClock:
    """确定性时钟：每次调用前进固定步长，保证测试与重放可复现。"""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00", step_seconds: int = 1):
        self._t = datetime.fromisoformat(start)
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> str:
        value = self._t.isoformat()
        self._t += self._step
        return value


class SystemClock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
