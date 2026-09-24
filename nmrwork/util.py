"""通用工具：稳定身份、规范化 JSON、指纹。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_id(prefix: str, *parts: Any) -> str:
    """由各组成部分生成稳定身份，相同输入永远得到相同 id。"""
    material = json.dumps(parts, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def fingerprint(payload: Any) -> str:
    material = canonical_json(payload)
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def parse_float(token: str) -> float:
    return round(float(token), 6)
