"""虚拟时钟：默认走真实墙钟；测试与重放可固定并按秒推进，保证可重放。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


class Clock:
    def __init__(self, fixed: Optional[str] = None) -> None:
        self._fixed: Optional[datetime] = (
            datetime.fromisoformat(fixed).replace(tzinfo=timezone.utc)
            if fixed is not None
            else None
        )
        self._offset = 0

    def now(self) -> str:
        if self._fixed is not None:
            return (self._fixed + timedelta(seconds=self._offset)).isoformat()
        return datetime.now(timezone.utc).isoformat()

    def set(self, iso: str) -> None:
        self._fixed = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        self._offset = 0

    def advance(self, seconds: int) -> str:
        self._offset += seconds
        return self.now()
