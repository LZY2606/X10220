from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_hash(prefix: str, *parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def event_id(project_id: str, branch_id: str, branch_seq: int) -> str:
    return stable_hash("evt", project_id, branch_id, branch_seq)


def candidate_id(atom_id: str, kind: str, peak_id: str | None = None) -> str:
    return stable_hash("cand", atom_id, kind, peak_id or "missing")


def candidate_result_id(atom_id: str, kind: str, peak_id: str | None, payload_hash: str) -> str:
    return stable_hash("cr", atom_id, kind, peak_id or "missing", payload_hash)


def value_id(source_id: str, record_id: str, field: str) -> str:
    return stable_hash("val", source_id, record_id, field)


def record_id(source_id: str, logical_id: str, row: int | None = None) -> str:
    if logical_id:
        return stable_hash("rec", source_id, logical_id)
    return stable_hash("rec", source_id, "row", row or 0)


def source_version_id(source_id: str, raw_content: str, filename: str) -> str:
    return stable_hash("srcver", source_id, raw_content, filename)


def parameter_id(params: dict[str, Any]) -> str:
    return stable_hash("param", params)
