"""导入层（ingest-1.0.0）。

原则：
- 原始输入一经提交不可覆盖；raw_text 与每一行原文完整保留。
- 原始表示（字符串）原样保留，规范化只发生在派生层字段 value/unit/basis。
- 无法转换的值留在原位置并附诊断，不为了画图丢弃。
- 明确的格式错误（坏魔数、缺头、列数不符、重复 id）抛出 IngestError。
"""
from __future__ import annotations

import hashlib
from typing import Any

INGEST_VERSION = "ingest-1.0.0"
MAGIC = "!nmrlite 1"

KIND_COLUMNS = {
    "peaklist-1h": ["id", "shift_ppm", "intensity", "integration", "multiplicity"],
    "peaklist-13c": ["id", "shift_ppm", "intensity", "integration", "multiplicity"],
    "correlations": ["id", "type", "from", "to"],
    "skeleton": ["id", "element", "label", "expected_ppm", "h_count", "h_ppm", "multiplicity", "sym", "x", "y"],
}

EDGE_TYPES = ("COSY", "HSQC", "HMBC")


class IngestError(Exception):
    """硬性格式错误：附带机器可读诊断列表。"""

    def __init__(self, diagnostics: list[dict[str, Any]]):
        self.diagnostics = diagnostics
        message = diagnostics[0]["message"] if diagnostics else "ingest error"
        super().__init__(message)


def _diag(code: str, message: str, line_no: int | None = None, field: str | None = None,
          raw: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"code": code, "message": message}
    if line_no is not None:
        d["line"] = line_no
    if field is not None:
        d["field"] = field
    if raw is not None:
        d["raw"] = raw
    return d


def _num_field(raw: str, unit: str, basis: str, field: str, line_no: int,
               diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    """数值字段：保留 raw，派生 value；无法转换时留在原位置并附诊断。"""
    try:
        value = float(raw)
        return {"raw": raw, "value": value, "unit": unit, "basis": basis, "ok": True}
    except (TypeError, ValueError):
        diagnostics.append(_diag(
            "UNCONVERTIBLE_NUMBER",
            f"第 {line_no} 行字段 {field} 的值 {raw!r} 无法转换为数值，已保留原始表示",
            line_no=line_no, field=field, raw=raw))
        return {"raw": raw, "value": None, "unit": unit, "basis": basis, "ok": False,
                "diagnostic": "UNCONVERTIBLE_NUMBER"}


def _int_field(raw: str, field: str, line_no: int,
               diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        value = int(raw)
        return {"raw": raw, "value": value, "unit": "count", "ok": True}
    except (TypeError, ValueError):
        diagnostics.append(_diag(
            "UNCONVERTIBLE_NUMBER",
            f"第 {line_no} 行字段 {field} 的值 {raw!r} 无法转换为整数，已保留原始表示",
            line_no=line_no, field=field, raw=raw))
        return {"raw": raw, "value": None, "unit": "count", "ok": False,
                "diagnostic": "UNCONVERTIBLE_NUMBER"}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_source(raw_text: str) -> dict[str, Any]:
    """解析 nmrlite/1 文本。硬错误抛 IngestError，软问题进 diagnostics。"""
    diagnostics: list[dict[str, Any]] = []
    lines = raw_text.splitlines()

    significant = [(i + 1, ln) for i, ln in enumerate(lines)
                   if ln.strip() and not ln.strip().startswith("#")]
    if not significant or significant[0][1].strip() != MAGIC:
        raise IngestError([_diag("BAD_MAGIC",
                                 f"首条有效行必须是 {MAGIC!r}（nmrlite/1 格式声明）")])

    headers: dict[str, str] = {}
    data_lines: list[tuple[int, str]] = []
    for line_no, line in significant[1:]:
        if line.strip().startswith("!"):
            parts = line.strip()[1:].split(None, 1)
            headers[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
        else:
            data_lines.append((line_no, line))

    kind = headers.get("kind")
    if not kind:
        raise IngestError([_diag("MISSING_HEADER", "缺少 '!kind' 头（peaklist-1h/peaklist-13c/correlations/skeleton）")])
    if kind not in KIND_COLUMNS:
        raise IngestError([_diag("UNKNOWN_KIND", f"未知 kind {kind!r}，支持：{sorted(KIND_COLUMNS)}")])
    columns = KIND_COLUMNS[kind]

    reference = headers.get("reference", "TMS")
    meta = {
        "solvent": {"raw": headers.get("solvent", ""), "value": headers.get("solvent") or None},
        "frequency_mhz": _num_field(headers["frequency_mhz"], "MHz", reference, "frequency_mhz", 0, diagnostics)
        if "frequency_mhz" in headers else
        {"raw": None, "value": None, "unit": "MHz", "basis": reference, "ok": False,
         "diagnostic": "MISSING_HEADER"},
        "reference": {"raw": reference, "value": reference},
    }
    if "frequency_mhz" not in headers:
        diagnostics.append(_diag("MISSING_HEADER", "缺少 '!frequency_mhz' 头，仪器频率未知"))

    peaks: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    atoms: list[dict[str, Any]] = []
    bonds: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def check_id(rid: str, line_no: int) -> None:
        if rid in seen_ids:
            raise IngestError([_diag("DUPLICATE_ID", f"第 {line_no} 行 id {rid!r} 重复", line_no=line_no, raw=rid)])
        seen_ids.add(rid)

    for line_no, line in data_lines:
        tokens = line.split()
        if kind == "skeleton" and tokens and tokens[0] == "bond":
            if len(tokens) != 3:
                raise IngestError([_diag(
                    "BAD_COLUMN_COUNT",
                    f"第 {line_no} 行 bond 需要 2 个原子 id，实际 {len(tokens) - 1} 个",
                    line_no=line_no, raw=line)])
            bonds.append({"bond_id": f"b{len(bonds) + 1:02d}", "a": tokens[1], "b": tokens[2],
                          "raw_line": line})
            continue
        if len(tokens) != len(columns):
            raise IngestError([_diag(
                "BAD_COLUMN_COUNT",
                f"第 {line_no} 行列数不符：期望 {len(columns)} 列（{' '.join(columns)}），实际 {len(tokens)} 列",
                line_no=line_no, raw=line)])
        rec = dict(zip(columns, tokens))
        check_id(rec["id"], line_no)

        if kind in ("peaklist-1h", "peaklist-13c"):
            peaks.append({
                "peak_id": rec["id"],
                "nucleus": "1H" if kind == "peaklist-1h" else "13C",
                "raw_line": line,
                "shift": _num_field(rec["shift_ppm"], "ppm", reference, "shift_ppm", line_no, diagnostics),
                "intensity": _num_field(rec["intensity"], "a.u.", "instrument", "intensity", line_no, diagnostics),
                "integration": _num_field(rec["integration"], "H", "spectrum", "integration", line_no, diagnostics),
                "multiplicity": {"raw": rec["multiplicity"], "value": rec["multiplicity"]},
            })
        elif kind == "correlations":
            etype = rec["type"].upper()
            type_field: dict[str, Any] = {"raw": rec["type"], "value": etype, "ok": True}
            if etype not in EDGE_TYPES:
                diagnostics.append(_diag(
                    "UNKNOWN_EDGE_TYPE",
                    f"第 {line_no} 行相关类型 {rec['type']!r} 未知（支持 COSY/HSQC/HMBC），已保留原始表示",
                    line_no=line_no, field="type", raw=rec["type"]))
                type_field = {"raw": rec["type"], "value": None, "ok": False,
                              "diagnostic": "UNKNOWN_EDGE_TYPE"}
            edges.append({
                "edge_id": rec["id"], "type": type_field,
                "from": rec["from"], "to": rec["to"], "raw_line": line,
            })
        elif kind == "skeleton":
            atoms.append({
                "atom_id": rec["id"],
                "element": rec["element"],
                "label": rec["label"],
                "expected_shift": _num_field(rec["expected_ppm"], "ppm", "expected", "expected_ppm", line_no, diagnostics),
                "h_count": _int_field(rec["h_count"], "h_count", line_no, diagnostics),
                "h_expected_shift": _num_field(rec["h_ppm"], "ppm", "expected", "h_ppm", line_no, diagnostics),
                "multiplicity_expected": {"raw": rec["multiplicity"], "value": rec["multiplicity"]},
                "symmetry_group": None if rec["sym"] in (".", "") else rec["sym"],
                "x": _num_field(rec["x"], "grid", "skeleton-2d", "x", line_no, diagnostics),
                "y": _num_field(rec["y"], "grid", "skeleton-2d", "y", line_no, diagnostics),
                "raw_line": line,
            })

    if not (peaks or edges or atoms):
        raise IngestError([_diag("EMPTY_DATA", "没有任何数据行")])

    return {
        "kind": kind,
        "meta": meta,
        "peaks": peaks,
        "edges": edges,
        "atoms": atoms,
        "bonds": bonds,
        "diagnostics": diagnostics,
        "algorithm": INGEST_VERSION,
    }
