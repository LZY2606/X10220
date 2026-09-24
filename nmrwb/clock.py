"""固定的虚拟时钟，保证重放与测试时间确定。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> str:  # pragma: no cover - 接口
        raise NotImplementedError


class VirtualClock(Clock):
    """每次调用返回一个 ISO 时间戳并步进固定秒数（默认 1 秒）。"""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00", step_seconds: int = 1):
        self._t = datetime.fromisoformat(start)
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> str:
        value = self._t.isoformat()
        self._t += self._step
        return value


class SystemClock(Clock):  # pragma: no cover - 真实时钟
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
