"""可注入的时钟，便于测试使用虚拟时间。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Clock:
    """单调时钟；测试可固定或推进。"""

    def __init__(self, fixed: Optional[str] = None) -> None:
        self._fixed = fixed
        self._override: Optional[Callable[[], str]] = None
        self._tick = 0

    def now(self) -> str:
        if self._override is not None:
            return self._override()
        if self._fixed is not None:
            base = datetime.fromisoformat(self._fixed)
            self._tick += 1
            return (
                base.replace(microsecond=0)
                .astimezone(timezone.utc)
                .isoformat()
            )
        return utcnow_iso()
