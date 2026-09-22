"""稳定身份与指纹。

身份规则：
- 文档身份：项目内按逻辑文件名确定，跨版本稳定。
- 记录身份：显式行标签优先，否则由 ``文档身份#行号#规范化内容`` 内容寻址。
  因此同一条谱峰在修订后内容不变时身份不变，真正的局部改动只改变被改记录。
- 结果身份：由输入记录身份集合、算法版本、参数指纹内容寻址。
- 身份在导出/删除数据库/再导入后保持不变。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

NAMESPACE = "urn:uuid:nmrb-workbench"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(parts: Iterable[Any]) -> str:
    """由任意可 JSON 化的部件生成稳定的内容摘要。"""
    payload = "\x1f".join(_canonical(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}_{digest(parts)[:16]}"


def doc_id(project_id: str, logical_name: str) -> str:
    return stable_id("doc", project_id, logical_name)


def lineage_id(project_id: str, logical_name: str) -> str:
    return stable_id("line", project_id, logical_name)


def record_id(doc_identity: str, lineno: int, label: str | None, raw: str) -> str:
    return stable_id("rec", doc_identity, lineno, label or "", raw.strip())


def result_id(nucleus: str, input_records: Mapping[str, Any], algo: str, fingerprint: str) -> str:
    return stable_id("res", nucleus, sorted(input_records.items()), algo, fingerprint)


def event_id(*parts: Any) -> str:
    return stable_id("evt", *parts)


def param_fingerprint(params: Mapping[str, Any]) -> str:
    return digest(sorted(params.items()))[:16]
