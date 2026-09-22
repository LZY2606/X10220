from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_id(kind: str, *parts) -> str:
    """Content-addressed, export/import-stable identifier."""
    digest = hashlib.sha1(canonical(list(parts)).encode("utf-8")).hexdigest()[:12]
    return "%s-%s" % (kind, digest)


def fingerprint(obj) -> str:
    return hashlib.sha1(canonical(obj).encode("utf-8")).hexdigest()[:16]


class Clock:
    """Real clock; tests inject VirtualClock for deterministic time."""

    def now(self) -> datetime:
        return datetime.utcnow()

    def iso(self) -> str:
        return self.now().replace(microsecond=0).isoformat() + "Z"


class VirtualClock(Clock):
    def __init__(self, start: str = "2026-01-01T00:00:00"):
        self.t = datetime.fromisoformat(start)

    def tick(self, seconds: int = 1) -> str:
        self.t += timedelta(seconds=seconds)
        return self.iso()

    def now(self) -> datetime:
        return self.t
