"""Stable identity helpers.

Identities are content hashes so export -> delete -> import reconstructs the
same ids, order and provenance links (independent of database rowids).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def hash_id(prefix: str, payload: Any) -> str:
    digest = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:12]}"


def fingerprint(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()
