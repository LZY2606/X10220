"""稳定身份:所有事件、run、候选、峰记录均使用内容哈希派生身份。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> str:
    """确定性 JSON,用于哈希、导出校验与导入复核。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def short_hash(obj: Any, length: int = 16) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()[:length]


def make_id(prefix: str, obj: Any, length: int = 16) -> str:
    return f"{prefix}_{short_hash(obj, length)}"
