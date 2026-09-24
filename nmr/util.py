"""确定性 ID、规范化 JSON 与虚拟时钟。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone


def sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Clock:
    """虚拟时钟：每次调用前进 1 秒，测试可完全复现。"""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00"):
        self.t = datetime.fromisoformat(start)

    def now(self) -> str:
        value = self.t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        self.t += timedelta(seconds=1)
        return value


class SystemClock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
