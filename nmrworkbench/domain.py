"""领域核心：稳定身份、原始值保留、单位/坐标基准、候选评分引擎。

设计约定
- 原始输入（raw）一经解析保留原字符串表示，规范化值放在 normalized 层；
  无法转换的值留在原位并附 diagnostic，绝不丢弃。
- 派生物的身份全部由内容决定（content-addressed），因此导出->删除->导入、
  以及任意时间点重放，身份、顺序、来源关系都保持。
- 重叠峰与缺峰不会被强制唯一匹配；耦合网络（COSY/HSQC/HMBC）只作为证据。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

ENGINE_VERSION = "nmr-assign-1.0.0"
FIXED_SEED = 1191

DEFAULT_PARAMS: Dict[str, Any] = {
    "global_bias_ppm": 0.0,
    "h_window_ppm": 0.25,
    "h_hard_window_ppm": 0.60,
    "c_window_ppm": 1.2,
    "c_hard_window_ppm": 3.0,
    "w_shift": 0.55,
    "w_hsqc": 0.20,
    "w_cosy": 0.10,
    "w_hmbc": 0.10,
    "w_multiplicity": 0.03,
    "w_integration": 0.02,
    "hmbc_radius_ppm": 0.08,
    "hsqc_radius_ppm": 0.08,
    "overlap_radius_ppm": 0.012,
    "tie_decimals": 5,
}

# ---------------------------------------------------------------------------
# 稳定身份与规范序列化
# ---------------------------------------------------------------------------

def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def cid(prefix: str, obj: Any) -> str:
    """内容寻址身份：同一内容永远得到同一 id。"""
    digest = hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()[:16]
    return "%s_%s" % (prefix, digest)


def param_fingerprint(params: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical(params).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 解析层：保留原始表示 + 规范化 + 诊断
# ---------------------------------------------------------------------------

class FormatError(Exception):
    def __init__(self, errors: List[Dict[str, str]]):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


def _as_float(value: Any) -> Tuple[Optional[float], Optional[str]]:
    if value is None:
        return None, "缺少数值"
    if isinstance(value, bool):
        return None, "布尔不是数值: %r" % value
    try:
        return float(value), None
    except (TypeError, ValueError):
        return None, "无法转换为数值: %r" % (value,)


def _doc_errors(parse: Dict[str, Any]) -> List[Dict[str, str]]:
    return [d for d in parse.get("diagnostics", []) if d.get("severity") == "error"]


def parse_document(kind: str, payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise FormatError([{"path": "$", "message": "顶层必须是对象"}])
    if kind == "peaks_h":
        return _parse_peaks(payload, "1H")
    if kind == "peaks_c":
        return _parse_peaks(payload, "13C")
    if kind == "edges":
        return _parse_edges(payload)
    if kind == "skeleton":
        return _parse_skeleton(payload)
    if kind == "context":
        return _parse_context(payload)
    raise FormatError([{"path": "$", "message": "未知文档类型 %s" % kind}])


def _parse_peaks(payload: Dict[str, Any], nucleus: str) -> Dict[str, Any]:
    diag: List[Dict[str, str]] = []
    rows_in = payload.get("peaks")
    if not isinstance(rows_in, list):
        raise FormatError([{"path": "peaks", "message": "peaks 必须是数组"}])
    rows: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for i, row in enumerate(rows_in):
        path = "peaks[%d]" % i
        if not isinstance(row, dict):
            raise FormatError([{"path": path, "message": "每行必须是对象"}])
        raw_shift = row.get("shift_ppm")
        shift, err = _as_float(raw_shift)
        if err:
            diag.append({"severity": "error", "path": path + ".shift_ppm", "message": err})
        rec: Dict[str, Any] = {
            "id": row.get("id"),
            "nucleus": nucleus,
            "raw": dict(row),
            "shift_ppm": shift,
            "shift_unit": "ppm",
            "shift_basis": "internal-reference",
        }
        if "intensity" in row:
            val, e = _as_float(row.get("intensity"))
            rec["intensity"] = val
            rec["intensity_unit"] = "counts"
            if e:
                diag.append({"severity": "error", "path": path + ".intensity", "message": e})
        if nucleus == "1H" and "integration" in row:
            val, e = _as_float(row.get("integration"))
            rec["integration"] = val
            rec["integration_unit"] = "proton-equivalents"
            if e:
                diag.append({"severity": "error", "path": path + ".integration", "message": e})
        rec["multiplicity"] = row.get("multiplicity") if isinstance(row.get("multiplicity"), str) else None
        if nucleus == "1H" and not rec["multiplicity"]:
            diag.append({"severity": "warning", "path": path + ".multiplicity",
                         "message": "缺少多重性，保留为空，不参与多重性评分"})
        rows.append(rec)
        key = canonical([nucleus, raw_shift, row.get("integration"), row.get("multiplicity")])
        seen[key] = seen.get(key, 0) + 1
    for i, row in enumerate(rows):
        if not row["id"]:
            row["id"] = "%s-pk-%02d" % ("h" if nucleus == "1H" else "c", i + 1)
    units = {"shift_ppm": "ppm"}
    if rows:
        if any("intensity" in r for r in rows):
            units["intensity"] = "counts"
        if nucleus == "1H" and any(r.get("integration") is not None for r in rows):
            units["integration"] = "proton-equivalents"
    return {"kind": "peaks_%s" % ("h" if nucleus == "1H" else "c"), "nucleus": nucleus,
            "rows": rows, "units": units, "diagnostics": diag}


_MULT_SET = {"s", "d", "t", "q", "dd", "dt", "td", "m", "br", "brs"}
