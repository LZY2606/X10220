"""可控时钟：测试使用虚拟时间，演示使用真实时间。"""
from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Optional


class Clock:
    def now_ms(self) -> int:
        return int(_time.time() * 1000)

    def iso(self) -> str:
        return datetime.fromtimestamp(self.now_ms() / 1000, tz=timezone.utc).isoformat()


class FixedClock(Clock):
    """从固定起点开始、每取一次推进一毫秒的虚拟时钟。"""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self._t = start_ms

    def now_ms(self) -> int:
        self._t += 1
        return self._t

    def fast_forward(self, ms: int) -> None:
        self._t += ms
