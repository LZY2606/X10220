"""稳定身份与规范化指纹。

身份在导出/删除本地数据库/再导入后保持稳定：身份只依赖领域内容
（项目名、原始行、父链、载荷规范化哈希），不依赖自增数据库主键。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> str:
    """把任意领域载荷序列化为确定性字节串。

    使用 sort_keys、固定分隔符与 ensure_ascii=False；浮点由调用方
    在放入载荷前统一 round，避免平台差异导致身份漂移。
    """
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest(payload: Any, prefix: str = "") -> str:
    text = canonical(payload)
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{prefix}{h[:16]}" if prefix else h[:16]


def chain_digest(prev_hash: str, event_type: str, payload: Any) -> str:
    """事件哈希链：事件身份同时依赖前一事件，构成可验证顺序。"""
    material = {"prev": prev_hash, "type": event_type, "payload": payload}
    return "ev_" + digest(material)


def fingerprint(params: dict) -> str:
    """算法参数指纹（短、稳定、可比较）。"""
    return "fp_" + digest(params)
