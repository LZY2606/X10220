"""Content hashing + stable identity helpers."""

from __future__ import annotations

import hashlib
import json


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def short_hash(obj: object, length: int = 12) -> str:
    return content_hash(obj).split(":", 1)[1][:length]


def stable_id(prefix: str, *parts: object) -> str:
    return f"{prefix}_{short_hash(parts)}"
