import time


class Clock:
    """真实时钟；服务器默认使用。"""

    def now(self) -> float:
        return time.time()


class VirtualClock(Clock):
    """虚拟时钟：测试专用，确定性地递增。"""

    def __init__(self, start: float = 1_700_000_000.0, step: float = 1.0):
        self._t = start
        self._step = step

    def now(self) -> float:
        value = self._t
        self._t += self._step
        return value
