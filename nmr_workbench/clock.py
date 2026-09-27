from datetime import datetime, timezone


class Clock:
    def now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ManualClock:
    def __init__(self, start: str = "2026-09-27T00:00:00Z"):
        self.current = start

    def now(self) -> str:
        return self.current

    def advance(self, seconds: int = 1) -> str:
        stamp = datetime.fromisoformat(self.current.replace("Z", "+00:00"))
        self.current = (
            stamp.replace(microsecond=0)
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return self.current
