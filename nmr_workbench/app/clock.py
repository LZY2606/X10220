"""Virtual clock.

All historical timestamps come from this deterministic, replayable clock so that
export/reimport and replays reproduce exactly.  Time never moves on its own:
``advance`` is invoked by the application service whenever a new domain event is
recorded (fixed step, optionally seeded).  Tests may pin the epoch/step.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

_EPOCH = _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
_STEP = _dt.timedelta(seconds=17)


@dataclass
class VirtualClock:
    epoch: _dt.datetime = _EPOCH
    step_seconds: int = 17
    ticks: int = 0

    def now(self) -> str:
        return (self.epoch + self.step * self.ticks).isoformat()

    @property
    def step(self) -> _dt.timedelta:
        return _dt.timedelta(seconds=self.step_seconds)

    def advance(self) -> str:
        self.ticks += 1
        return self.now()

    def state(self) -> dict:
        return {"epoch": self.epoch.isoformat(),
                "step_seconds": self.step_seconds, "ticks": self.ticks}

    @classmethod
    def from_state(cls, state: dict | None) -> "VirtualClock":
        if not state:
            return cls()
        return cls(epoch=_dt.datetime.fromisoformat(state["epoch"]),
                   step_seconds=state["step_seconds"], ticks=state["ticks"])
