"""时间源：生产用 UTC 系统钟，测试/案例包用可步进的虚拟钟（固定 seed 复放）。"""

from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    def now_iso(self) -> str:
        raise NotImplementedError


class SystemClock(Clock):
    def now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FixedClock(Clock):
    """按给定 ISO 字符串序列依次发牌的虚拟钟；用完后停在最后一个时刻。"""

    def __init__(self, moments: list[str]):
        if not moments:
            raise ValueError("FixedClock 需要至少一个时刻")
        self._moments = list(moments)
        self._idx = 0

    def now_iso(self) -> str:
        moment = self._moments[min(self._idx, len(self._moments) - 1)]
        if self._idx < len(self._moments) - 1:
            self._idx += 1
        return moment
