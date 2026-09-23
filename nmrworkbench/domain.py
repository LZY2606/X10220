"""导入解析与规范化。

原则：原始表示（raw text）永远保留；本模块是派生层，负责把
化学位移 / 峰强 / 积分 / 多重度 / 条件头规范化为数值，
无法转换的值留在原位置并附诊断，绝不为了画图而丢弃。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

ALGORITHM_VERSION = "nmrwb-assign/1.2.0"
FORMAT_1D = "nmrwb-1d"
FORMAT_2D = "nmrwb-2d"
FORMAT_MOL = "nmrwb-molecule/1"
EXPORT_FORMAT = "nmrwb-export/1"

CONDITION_KEYS = ("solvent", "frequency", "temperature", "reference")


class ImportFormatError(ValueError):
    """明确的格式错误：向调用方返回全部诊断行。"""

    def __init__(self, diagnostics):
        self.diagnostics = list(diagnostics)
        super().__init__("; ".join(self.diagnostics))


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:16]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class LogicalClock:
    """虚拟时钟：逻辑时间 = 基准时刻 + seq 秒，完全确定、可在测试中固定。"""

    def __init__(self, base: str = "2026-01-01T00:00:00+00:00"):
        self.base = datetime.fromisoformat(base)
        if self.base.tzinfo is None:
            self.base = self.base.replace(tzinfo=timezone.utc)

    def at(self, seq: int) -> str:
        t = self.base + timedelta(seconds=int(seq))
        return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_float(raw: str):
    try:
        return float(raw), None
    except (TypeError, ValueError):
        return None, f"unconvertible_number:{raw!r}"


def _parse_header_line(line: str):
    if ":" not in line:
        return None, None
    key, _, value = line.partition(":")
    return key.strip(), value.strip()


def parse_conditions(header: dict):
    """从 1D 头解析溶剂 / 仪器频率 / 温度 / 参考标准，保留 raw。"""
    out = []
    for key in CONDITION_KEYS:
        if key not in header:
            continue
        raw = header[key]
        value, unit, diag = None, "", None
        if key == "frequency":
            parts = raw.split()
            value, diag = _to_float(parts[0]) if parts else (None, "empty")
            unit = parts[1] if len(parts) > 1 else "MHz"
            if unit != "MHz" and diag is None:
                diag = f"unexpected_unit:{unit!r}"
        elif key == "temperature":
            parts = raw.split()
            value, diag = _to_float(parts[0]) if parts else (None, "empty")
            unit = parts[1] if len(parts) > 1 else "K"
            if unit != "K" and diag is None:
                diag = f"unexpected_unit:{unit!r}"
        out.append({"key": key, "raw": raw, "value": value, "unit": unit, "diag": diag})
    return out


def parse_1d(text: str):
    """解析一维 peak list。返回 {header, conditions, peaks, diagnostics}。"""
    if not text.lstrip().startswith("# nmrwb-1d"):
        raise ImportFormatError(["第一行必须是 '# nmrwb-1d v1' 魔数行"])
    header, peaks, diagnostics = {}, [], []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("peak "):
            tokens = line.split()
            if len(tokens) < 4:
                diagnostics.append(f"line {lineno}: peak 行字段不足")
                continue
            label, shift_raw, unit = tokens[1], tokens[2], tokens[3]
            kv = {}
            for tok in tokens[4:]:
                if "=" in tok:
                    k, _, v = tok.partition("=")
                    kv[k.strip()] = v.strip()
                else:
                    diagnostics.append(f"line {lineno}: 无法识别的片段 {tok!r}")
            row = {"label": label, "shift_raw": f"{shift_raw} {unit}",
                   "intensity_raw": kv.get("intensity"),
                   "integration_raw": kv.get("integration"),
                   "multiplicity_raw": kv.get("mult"),
                   "shift_ppm": None, "intensity": None, "integration": None,
                   "diag": []}
            if unit != "ppm":
                row["diag"].append(f"unexpected_shift_unit:{unit!r}")
            else:
                row["shift_ppm"], d = _to_float(shift_raw)
                if d:
                    row["diag"].append(f"unconvertible_shift:{shift_raw!r}")
            if row["intensity_raw"] is not None:
                row["intensity"], d = _to_float(row["intensity_raw"])
                if d:
                    row["diag"].append(f"unconvertible_intensity:{row['intensity_raw']!r}")
            if row["integration_raw"] is not None:
                raw_int = row["integration_raw"]
                trimmed = raw_int[:-1] if raw_int.upper().endswith(("H", "C")) else raw_int
                row["integration"], d = _to_float(trimmed)
                if d:
                    row["diag"].append(f"unconvertible_integration:{raw_int!r}")
            peaks.append(row)
        else:
            key, value = _parse_header_line(line)
            if key is None:
                diagnostics.append(f"line {lineno}: 无法识别的行 {line!r}")
            else:
                header[key] = value
    if "nucleus" not in header:
        raise ImportFormatError(["缺少 nucleus: 头行"])
    if not peaks:
        raise ImportFormatError(["文件不含任何 peak 行"])
    return {"header": header, "conditions": parse_conditions(header),
            "peaks": peaks, "diagnostics": diagnostics}


def parse_2d(text: str):
    """解析二维相关边（COSY/HSQC/HMBC）。"""
    if not text.lstrip().startswith("# nmrwb-2d"):
        raise ImportFormatError(["第一行必须是 '# nmrwb-2d v1' 魔数行"])
    header, edges, diagnostics = {}, [], []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("edge "):
            tokens = line.split()
            if len(tokens) != 3:
                diagnostics.append(f"line {lineno}: edge 行必须是 'edge <a> <b>'")
                continue
            edges.append({"a": tokens[1], "b": tokens[2], "raw": raw_line.strip()})
        else:
            key, value = _parse_header_line(line)
            if key is None:
                diagnostics.append(f"line {lineno}: 无法识别的行 {line!r}")
            else:
                header[key] = value
    kind = header.get("kind", "").upper()
    if kind not in ("COSY", "HSQC", "HMBC"):
        raise ImportFormatError([f"kind 必须是 COSY/HSQC/HMBC，得到 {header.get('kind')!r}"])
    if not edges:
        raise ImportFormatError(["文件不含任何 edge 行"])
    return {"kind": kind, "edges": edges, "diagnostics": diagnostics}


def parse_molecule(text: str):
    """解析分子骨架（JSON），位点带期望位移 / 积分 / 多重度 / 对称组。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportFormatError([f"分子骨架不是合法 JSON: {exc}"])
    if data.get("format") != FORMAT_MOL:
        raise ImportFormatError([f"format 必须是 {FORMAT_MOL!r}"])
    sites = data.get("sites")
    if not isinstance(sites, list) or not sites:
        raise ImportFormatError(["sites 必须是非空数组"])
    out, diagnostics = [], []
    for i, s in enumerate(sites):
        site = {"site": s.get("site"), "element": s.get("element"),
                "expected_ppm": s.get("expected_ppm"),
                "integration": s.get("integration"),
                "multiplicity": s.get("multiplicity"),
                "carbon": s.get("carbon"),
                "sym_group": s.get("sym"),
                "neighbors": list(s.get("neighbors") or [])}
        if not site["site"] or not site["element"]:
            diagnostics.append(f"sites[{i}]: 缺少 site/element")
            continue
        out.append(site)
    if not out:
        raise ImportFormatError(["没有可用位点"] + diagnostics)
    return {"name": data.get("name", ""), "sites": out, "diagnostics": diagnostics}


DEFAULT_PARAMS = {
    "algorithm": ALGORITHM_VERSION,
    "tol_shift_ppm": 0.15,
    "tol_c_shift_ppm": 2.0,
    "overlap_tol_ppm": 0.03,
    "tol_integration_rel": 0.30,
    "offset_warn_ppm": 0.05,
    "weights": {"shift": 1.0, "integration": 0.4, "multiplicity": 0.3, "evidence": 0.6},
}
