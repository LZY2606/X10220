"""Stable identity helpers.

Identities are content/lineage derived (never DB rowids), so they survive
export -> delete database -> import unchanged.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canon(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha12(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def hash_chain(prev_hash: str, branch_id: str, seq: int, etype: str, payload: Any) -> str:
    body = f"{prev_hash}|{seq}|{branch_id}|{etype}|{canon(payload)}"
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def obj_id(source_id: str, local: str) -> str:
    return f"{source_id}#{local}"


def candidate_id(run_id: str, atom_id: str, peak_id: str | None) -> str:
    return f"cd:{run_id.split(':', 1)[1]}:{atom_id}:{peak_id or 'unknown'}"
