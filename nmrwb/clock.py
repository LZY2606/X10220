"""Virtual, injectable clock.

All timestamps in the system come from here so that tests can pin time
and replays stay deterministic.  No wall-clock or network time is used.
"""

import datetime


class VirtualClock:
    def __init__(self, start: str = "2026-01-01T00:00:00Z"):
        self.start = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
        self.ticks = 0

    def now(self) -> str:
        self.ticks += 1
        moment = self.start + datetime.timedelta(seconds=self.ticks)
        return moment.isoformat().replace("+00:00", "Z")


_clock = VirtualClock()


def set_clock(clock: VirtualClock) -> None:
    global _clock
    _clock = clock


def now() -> str:
    return _clock.now()
