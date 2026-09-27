"""稳定身份：所有身份由规范化内容哈希得到，导出/删除/再导入后保持。"""

from __future__ import annotations

import hashlib
import json
import uuid


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha8(prefix: str, *parts) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, (dict, list)):
            digest.update(canonical(part))
        else:
            digest.update(str(part).encode("utf-8"))
        digest.update(b"\x1f")
    return f"{prefix}_{digest.hexdigest()[:10]}"


def source_id(filename: str, version: int, raw_text: str) -> str:
    return sha8("src", filename, version, raw_text)


def doc_id(kind: str, version: str, raw_text: str) -> str:
    return sha8("doc", kind, version, raw_text)


def event_id(seq: int, parent: str | None, etype: str, payload: dict, actor: str, at: str) -> str:
    return sha8("evt", seq, parent or "ROOT", etype, payload, actor, at)


def run_id(input_hash: dict) -> str:
    return sha8("run", input_hash)


def option_id(run: str, site: str, peak_key: str | None, state: str) -> str:
    return sha8("opt", run, site, peak_key or "NONE", state)


def branch_id(name: str) -> str:
    return sha8("br", name)


def conflict_id(a: str, b: str) -> str:
    lo, hi = sorted((a, b))
    return sha8("cfl", lo, hi)


def new_project_id() -> str:
    return "prj_" + uuid.uuid4().hex[:12]
