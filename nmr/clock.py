"""虚拟时钟：测试与重放使用固定时间，默认使用真实 UTC 时间。"""
from __future__ import annotations

import datetime as _dt
from typing import Optional


class Clock:
    """可冻结的 UTC 时钟，时间戳使用 ISO-8601 带 Z 后缀，便于机器读取。"""

    def __init__(self, frozen: Optional[str] = None) -> None:
        self._frozen = frozen

    def set(self, iso_utc: str) -> None:
        self._frozen = iso_utc

    def advance(self, seconds: int) -> str:
        base = self._frozen or _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        dt = _dt.datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        ) + _dt.timedelta(seconds=seconds)
        self._frozen = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._frozen

    def now(self) -> str:
        if self._frozen is not None:
            return self._frozen
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
