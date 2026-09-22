"""NMR peak list 文本导入解析器。

格式(小型、可复制):

    # 注释
    @source proton                 # 逻辑来源标识;同名文件构成版本链
    @section conditions
    solvent: CDCl3
    @section peaks
    @begin 1H
    4.09 | 8.1e2 | 2H | q
    @end
    @section hsqc
    H 4.09 <-> C 60.65

解析失败的行:记录保留在原位置(带 line_no/raw),并附诊断;不丢弃任何输入。
记录 uid 由(逻辑文件名、段、行内容)内容哈希得到,
因此新版本中未改动的行保持同一稳定身份。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from . import units
from .ids import short_hash

SECTIONS = {"conditions", "peaks", "cosy", "hsqc", "hmbc", "notes"}
SPECTRA = {"1H", "13C"}
EDGE_SECTIONS = {"cosy": "COSY", "hsqc": "HSQC", "hmbc": "HMBC"}
_EDGE_TOKEN_RE = re.compile(r"^(?:(H|C)\s+)?(-?\d+(?:\.\d+)?)$")


def _diag(code: str, message: str, line_no: int = None) -> Dict[str, Any]:
    item = {"code": code, "message": message}
    if line_no is not None:
        item["line_no"] = line_no
    return item


def _parse_peak(line: str, spectrum: str, line_no: int) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "kind": "peak", "raw": line, "line_no": line_no, "spectrum": spectrum,
        "ok": False, "shift": None, "intensity": None, "integral": None,
        "multiplicity": None, "diagnostics": [],
    }
    if spectrum is None:
        rec["diagnostics"].append(
            _diag("missing_spectrum", "峰行缺少 @begin 1H|13C 声明", line_no)
        )
        return rec
    fields = [part.strip() for part in line.split("|")]
    if len(fields) != 4:
        rec["diagnostics"].append(
            _diag(
                "bad_peak_fields",
                f"峰行需要 4 个字段 shift|intensity|integral|multiplicity,实得 {len(fields)};原值保留",
                line_no,
            )
        )
        return rec
    shift = units.parse_shift(fields[0])
    intensity = units.parse_intensity(fields[1])
    integral = units.parse_integral(fields[2])
    multiplicity = units.parse_multiplicity(fields[3])
    rec["shift"] = shift
    rec["intensity"] = intensity
    rec["integral"] = integral
    rec["multiplicity"] = multiplicity
    for sub in (shift, intensity, integral, multiplicity):
        if sub.get("diagnostic"):
            diagnostic = dict(sub["diagnostic"])
            diagnostic["line_no"] = line_no
            rec["diagnostics"].append(diagnostic)
    rec["ok"] = all(
        sub.get("ok") for sub in (shift, intensity, integral, multiplicity)
    )
    return rec


def _parse_edge(edge_type: str, line: str, line_no: int) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "kind": "edge", "edge_type": edge_type, "raw": line, "line_no": line_no,
        "ok": False, "endpoints": [], "note": None, "diagnostics": [],
    }
    parts = [part.strip() for part in line.split("|")]
    if len(parts) > 1 and parts[1]:
        rec["note"] = parts[1]
    tokens = [part.strip() for part in parts[0].split("<->")]
    if len(tokens) != 2:
        rec["diagnostics"].append(
            _diag("bad_edge_line", "相关边需要 'a <-> b' 形式;原值保留", line_no)
        )
        return rec
    defaults = ("H", "H") if edge_type == "COSY" else ("H", "C")
    endpoints: List[Dict[str, Any]] = []
    ok = True
    for token, default in zip(tokens, defaults):
        match = _EDGE_TOKEN_RE.match(token)
        if not match:
            ok = False
            rec["diagnostics"].append(
                _diag("bad_edge_endpoint", f"端点无法解析: {token!r};原值保留", line_no)
            )
            endpoints.append({"raw": token, "element": None, "shift_ppm": None})
            continue
        element = match.group(1) or default
        endpoints.append(
            {
                "raw": token,
                "element": "1H" if element == "H" else "13C",
                "shift_ppm": float(match.group(2)),
            }
        )
    rec["endpoints"] = endpoints
    rec["ok"] = ok
    return rec


def _assign_uids(filename: str, source_name: str, records: List[Dict[str, Any]]) -> None:
    seen: Dict[str, int] = {}
    for rec in records:
        prefix = {"peak": "pk", "edge": "ed", "note": "nt"}[rec["kind"]]
        base = [source_name or filename, rec["kind"], rec.get("edge_type"), rec["raw"]]
        digest = short_hash(base, 12)
        count = seen.get(digest, 0) + 1
        seen[digest] = count
        if count > 1:
            digest = short_hash(base + [count], 12)
        rec["uid"] = f"{prefix}_{digest}"


def parse_source(filename: str, text: str) -> Dict[str, Any]:
    """解析一个来源文件;返回记录、条件键值(原始字符串)与诊断。"""
    records: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []
    conditions: Dict[str, str] = {}
    section = None
    spectrum = None
    source_name = None
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("@source"):
            parts = stripped.split(None, 1)
            if len(parts) > 1:
                source_name = parts[1].strip()
            continue
        if stripped.startswith("@section"):
            parts = stripped.split(None, 1)
            name = parts[1].strip().lower() if len(parts) > 1 else ""
            if name not in SECTIONS:
                diagnostics.append(
                    _diag("unknown_section", f"未知段 '@{name}';该行保留为诊断", line_no)
                )
                section = None
            else:
                section = name
            spectrum = None
            continue
        if stripped.startswith("@begin"):
            parts = stripped.split(None, 1)
            spec = parts[1].strip() if len(parts) > 1 else ""
            if spec not in SPECTRA:
                diagnostics.append(
                    _diag("unknown_spectrum", f"未知谱型 {spec!r};声明保留为诊断", line_no)
                )
            else:
                spectrum = spec
            continue
        if stripped.startswith("@end"):
            spectrum = None
            continue
        if section == "conditions":
            if ":" not in stripped:
                diagnostics.append(
                    _diag("bad_condition_line", "条件行需要 'key: value';原值保留", line_no)
                )
                continue
            key, value = stripped.split(":", 1)
            conditions[key.strip().lower()] = value.strip()
            continue
        if section == "peaks":
            records.append(_parse_peak(stripped, spectrum, line_no))
            continue
        if section in EDGE_SECTIONS:
            records.append(_parse_edge(EDGE_SECTIONS[section], stripped, line_no))
            continue
        if section == "notes":
            records.append(
                {"kind": "note", "raw": stripped, "line_no": line_no, "ok": True}
            )
            continue
        diagnostics.append(
            _diag(
                "line_outside_section",
                "该行不在任何已知段内;内容保留为诊断(不丢弃输入)",
                line_no,
            )
        )
    _assign_uids(filename, source_name, records)
    return {
        "filename": filename,
        "source_name": source_name or filename,
        "records": records,
        "conditions": conditions,
        "diagnostics": diagnostics,
    }
