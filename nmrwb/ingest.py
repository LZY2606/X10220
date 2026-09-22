"""导入层。

原始表示一律保留（meta 字符串、peak/edge 行号与原始字段）；
只有派生层才做数值规范化；无法转换的值留在原位置并附诊断，
不能为了画图而丢掉。
"""
from __future__ import annotations

from typing import Dict, List

from .core import stable_id

FORMATS = ("nmrwb-mol", "nmrwb-1d", "nmrwb-2d")


class IngestError(Exception):
    """明确的格式错误（未知格式/缺文件头/声明不符）。"""


def _to_float(raw, diag: List[Dict], line: int, field: str):
    try:
        return float(raw)
    except (TypeError, ValueError):
        diag.append({"line": line, "field": field, "raw": raw,
                     "message": "无法转换为数值: %r，原始表示保留" % raw})
        return None


def parse_input(name: str, fmt: str, raw: str) -> Dict:
    if fmt not in FORMATS:
        raise IngestError("格式错误: 未知格式 %r，支持 %s" % (fmt, list(FORMATS)))
    lines = raw.splitlines()
    header = next((l.strip() for l in lines if l.strip()), "")
    if not header.startswith("# nmrwb-"):
        raise IngestError("格式错误: 缺少 '# nmrwb-<kind> v<n>' 文件头")
    if not header.startswith("# " + fmt):
        raise IngestError("格式错误: 文件头 %r 与声明格式 %r 不一致" % (header, fmt))
    diag: List[Dict] = []
    if fmt == "nmrwb-mol":
        parsed = _parse_mol(lines, diag)
    elif fmt == "nmrwb-1d":
        parsed = _parse_1d(lines, diag)
    else:
        parsed = _parse_2d(lines, diag)
    return {"name": name, "format": fmt, "parsed": parsed, "diagnostics": diag}


def _parse_1d(lines, diag):
    meta = {}
    peaks = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if parts[0] == "peak":
            if len(parts) < 5:
                diag.append({"line": i, "field": "peak", "raw": s,
                             "message": "peak 行需要 4 个字段: shift intensity integration multiplicity"})
                continue
            peaks.append({
                "peak_id": stable_id("peak", i, parts[1], parts[4]),
                "line": i,
                "shift_raw": parts[1], "intensity_raw": parts[2],
                "integration_raw": parts[3], "multiplicity": parts[4],
                "shift": _to_float(parts[1], diag, i, "shift"),
                "intensity": _to_float(parts[2], diag, i, "intensity"),
                "integration": _to_float(parts[3], diag, i, "integration"),
            })
        elif ":" in s:
            k, _, v = s.partition(":")
            meta[k.strip()] = v.strip()
        else:
            diag.append({"line": i, "field": None, "raw": s,
                         "message": "无法识别的行，原样保留"})
    if "nucleus" not in meta:
        diag.append({"line": 0, "field": "nucleus", "raw": "",
                     "message": "缺少 nucleus 元数据"})
    return {"meta": meta, "peaks": peaks}


def _parse_mol(lines, diag):
    atoms = []
    bonds = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if parts[0] == "atom":
            if len(parts) < 4:
                diag.append({"line": i, "field": "atom", "raw": s,
                             "message": "atom 行需要: id element lo-hi [sym=g] [@ x y]"})
                continue
            aid, element, rangestr = parts[1], parts[2], parts[3]
            lo = hi = None
            if "-" in rangestr:
                a, _, b = rangestr.partition("-")
                lo = _to_float(a, diag, i, "range_lo")
                hi = _to_float(b, diag, i, "range_hi")
            else:
                diag.append({"line": i, "field": "range", "raw": rangestr,
                             "message": "化学位移范围应为 lo-hi (ppm)"})
            sym = None
            x = y = None
            rest = parts[4:]
            j = 0
            while j < len(rest):
                if rest[j].startswith("sym="):
                    sym = rest[j][4:]
                elif rest[j] == "@" and j + 2 < len(rest):
                    x = _to_float(rest[j + 1], diag, i, "x")
                    y = _to_float(rest[j + 2], diag, i, "y")
                    j += 2
                j += 1
            atoms.append({
                "atom_id": aid, "element": element,
                "range_raw": rangestr,
                "range": [lo, hi] if lo is not None and hi is not None else None,
                "sym": sym, "x": x, "y": y, "line": i,
            })
        elif parts[0] == "bond" and len(parts) >= 3:
            bonds.append({"bond_id": stable_id("bond", parts[1], parts[2]),
                          "a": parts[1], "b": parts[2], "line": i})
        else:
            diag.append({"line": i, "field": None, "raw": s,
                         "message": "无法识别的行，原样保留"})
    return {"atoms": atoms, "bonds": bonds}


def _parse_2d(lines, diag):
    meta = {}
    edges = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if parts[0] == "edge":
            if len(parts) < 3:
                diag.append({"line": i, "field": "edge", "raw": s,
                             "message": "edge 行需要至少 2 个频率坐标"})
                continue
            note = parts[3] if len(parts) > 3 else ""
            edges.append({
                "edge_id": stable_id("edge", i, parts[1], parts[2]),
                "line": i, "f1_raw": parts[1], "f2_raw": parts[2], "note": note,
                "f1": _to_float(parts[1], diag, i, "f1"),
                "f2": _to_float(parts[2], diag, i, "f2"),
            })
        elif ":" in s:
            k, _, v = s.partition(":")
            meta[k.strip()] = v.strip()
        else:
            diag.append({"line": i, "field": None, "raw": s,
                         "message": "无法识别的行，原样保留"})
    if "type" not in meta:
        diag.append({"line": 0, "field": "type", "raw": "",
                     "message": "缺少 type 元数据 (COSY/HSQC/HMBC)"})
    return {"meta": meta, "edges": edges}
