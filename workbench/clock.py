"""可注入时钟：运行时用系统时间，测试用虚拟时间。"""
import datetime


class SystemClock:
    def now(self):
        return datetime.datetime.now(datetime.timezone.utc).isoformat()


class VirtualClock:
    """固定起点、每次调用前进固定步长的确定性时钟。"""

    def __init__(self, start="2026-01-01T00:00:00+00:00", step_seconds=1):
        self._t = datetime.datetime.fromisoformat(start)
        self._step = datetime.timedelta(seconds=step_seconds)

    def now(self):
        value = self._t.isoformat()
        self._t += self._step
        return value
