"""虚拟时钟：测试可注入固定时间，运行时使用真实 UTC 时间。"""
from datetime import datetime, timezone

_now = None


def set_now(value):
    """value: ISO 字符串或 None（恢复真实时间）。"""
    global _now
    _now = value


def now_iso():
    if _now is not None:
        return _now
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
