"""稳定身份（stable id）与内容指纹工具。

身份与插入顺序无关：全部由项目 id 与内容的规范化 JSON + sha256 派生，
因此导出 -> 删除本地数据库 -> 导入后身份、输入顺序与来源关系保持不变。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:16]


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def source_id(project_id: str, kind: str, version: int, filename: str) -> str:
    return f"src_{content_hash([project_id, kind, version, filename])}"


def peak_id(project_id: str, source_id: str, index: int, peak_key: str) -> str:
    return f"pk_{content_hash([project_id, source_id, index, peak_key])}"


def edge_id(project_id: str, source_id: str, index: int, edge_key: str) -> str:
    return f"ed_{content_hash([project_id, source_id, index, edge_key])}"


def atom_id(project_id: str, label: str) -> str:
    return f"at_{content_hash([project_id, label])}"


def paramset_id(project_id: str, version: int) -> str:
    return f"pm_{content_hash([project_id, 'params', version])}"


def condition_id(project_id: str, atom_source_ver: int, param_version: int, meta_hash: str) -> str:
    return f"cn_{content_hash([project_id, atom_source_ver, param_version, meta_hash])}"


def candidate_id(project_id: str, peak_key: str, nucleus: str, atom_label: str,
                 condition: str) -> str:
    return f"cd_{content_hash([project_id, peak_key, nucleus, atom_label, condition])}"


def unknown_id(project_id: str, atom_label: str, nucleus: str, condition: str) -> str:
    return f"un_{content_hash([project_id, atom_label, nucleus, condition])}"


def event_id(project_id: str, seq: int, event_hash: str) -> str:
    return f"ev_{content_hash([project_id, seq, event_hash])}"


def decision_id(project_id: str, event_hash: str) -> str:
    return f"dc_{content_hash([project_id, event_hash])}"


def stable_order_key(record: Dict[str, Any]) -> List[Any]:
    """记录的确定性排序键：源版本 -> 源类型 -> 文件 -> 文件内序号。"""
    return [
        record.get("source_version", 0),
        str(record.get("source_kind", "")),
        str(record.get("filename", "")),
        record.get("file_index", 0),
        str(record.get("stable_id", "")),
    ]


def sort_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(records, key=stable_order_key)
