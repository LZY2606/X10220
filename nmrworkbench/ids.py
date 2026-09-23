"""稳定身份:所有领域对象使用内容寻址 ID,导出/导入后保持不变。"""
from __future__ import annotations

import hashlib
import json


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def content_id(kind: str, payload) -> str:
    return "%s:%s" % (kind, short_hash(canonical(payload)))
