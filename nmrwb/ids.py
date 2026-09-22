"""Stable identity helpers.

Every displayed value carries a stable id.  Ids are content-based (BLAKE2b
truncated hashes) plus an explicit human-readable key.  Ordering is encoded
into sequence numbers so export -> delete -> import preserves both identity
and order.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional


def digest(value: Any) -> str:
    """Deterministic short BLAKE2b digest over a JSON-serialisable value."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return "%s-%s" % (prefix, digest(list(parts)))


def fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def ordered(seq: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for index, item in enumerate(seq):
        item = dict(item)
        item.setdefault("ord", index)
        out.append(item)
    return out
