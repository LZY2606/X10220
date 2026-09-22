"""虚拟时钟：所有时间戳来自可重置的虚拟钟，测试可固定时间。"""
from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self, start="2026-01-01T00:00:00+00:00"):
        self._start = datetime.fromisoformat(start)
        if self._start.tzinfo is None:
            self._start = self._start.replace(tzinfo=timezone.utc)
        self._n = 0

    def now_iso(self):
        self._n += 1
        return (self._start + timedelta(seconds=self._n)).isoformat()


CLOCK = Clock()


def now_iso():
    return CLOCK.now_iso()


def reset_clock(start="2026-01-01T00:00:00+00:00"):
    global CLOCK
    CLOCK = Clock(start)
