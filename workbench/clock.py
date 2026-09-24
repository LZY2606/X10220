"""Virtual clock. Tests inject a controller; production reads wall time."""

from __future__ import annotations

from datetime import datetime, timezone

_NOW: "TimeController | None" = None


class TimeController:
    """Deterministic clock used by tests (never sleeps, never network)."""

    def __init__(self, start: str = "2026-01-01T00:00:00Z"):
        self._t = datetime.fromisoformat(start.replace("Z", "+00:00"))
        self._seq = 0

    def advance(self, seconds: int = 1) -> None:
        self._t = self._t.replace(microsecond=0)
        self._t = self._t.timestamp() and datetime.fromtimestamp(
            self._t.timestamp() + seconds, tz=timezone.utc
        )

    def now_iso(self) -> str:
        return self._t.strftime("%Y-%m-%dT%H:%M:%SZ")

    def tick(self) -> int:
        self._seq += 1
        return self._seq


def set_controller(controller: TimeController | None) -> None:
    global _NOW
    _NOW = controller


def now_iso() -> str:
    if _NOW is not None:
        return _NOW.now_iso()
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
