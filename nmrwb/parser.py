"""NMR peak list 文本格式（nmr-list-v1）解析层。

设计原则（对应验收）：
- 原始输入逐行保留（raw_text + 每行 raw_line/lineno），一经提交不可覆盖。
- 所有领域量携带单位或坐标基准；导入时保留原始 token，派生层才规范化。
- 无法转换的值留在原位置并附诊断，绝不因画图而丢弃。
- error 阻断提交，warning 不阻断；提示定位到段/行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .ident import digest

MULTIPLICITIES = {
    "s", "d", "t", "q", "p", "h", "hept", "m", "br", "n",
    "dd", "dt", "td", "dq", "ddd", "dddd", "AB", "AA'BB'",
}
SECTION_SET = {"@" + s for s in
               ("meta", "skeleton", "predict", "1H", "13C",
                "COSY", "HSQC", "HMBC")}
EDGE_KINDS = ("COSY", "HSQC", "HMBC")


@dataclass
class Diagnostic:
    level: str
    code: str
    section: str
    lineno: int
    message: str
    token: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "code": self.code, "section": self.section,
                "lineno": self.lineno, "message": self.message, "token": self.token}


@dataclass
class Row:
    lineno: int
    raw_line: str
    tokens: List[str]
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"lineno": self.lineno, "raw_line": self.raw_line,
                "tokens": self.tokens, "data": self.data}


@dataclass
class ParsedDocument:
    format: str
    raw_text: str
    meta: Dict[str, str]
    rows: Dict[str, List[Row]]
    diagnostics: List[Diagnostic]
    source_hash: str

    @property
    def has_errors(self) -> bool:
        return any(d.level == "error" for d in self.diagnostics)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format, "raw_text": self.raw_text, "meta": self.meta,
            "rows": {k: [r.to_dict() for r in v] for k, v in self.rows.items()},
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "source_hash": self.source_hash,
        }


def _num(tok: str) -> Optional[float]:
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


def _shift(tok: str) -> Dict[str, Any]:
    """'7.26' / '7.26ppm'：保留原始表示、单位与规范化 ppm 值。"""
    out = {"raw": tok, "value": None, "unit": None, "scale": "ppm_relative_TMS"}
    body = tok
    for u in ("ppm", "Ppm", "PPM"):
        if body.endswith(u):
            body = body[: -len(u)]
            out["unit"] = "ppm"
            break
    val = _num(body)
    if val is not None:
        out["value"] = val
        if out["unit"] is None:
            out["unit"] = "ppm"
    return out


def parse_peak_list(raw_text: str) -> ParsedDocument:
    diags: List[Diagnostic] = []
    sections = ["meta", "skeleton", "predict", "1H", "13C",
                "COSY", "HSQC", "HMBC"]
    rows: Dict[str, List[Row]] = {s: [] for s in sections}
    meta: Dict[str, str] = {}
    fmt = "nmr-list-v1"
    current = ""

    lines = raw_text.splitlines()
    if not lines:
        diags.append(Diagnostic("error", "EMPTY_DOCUMENT", "", 0, "空文档"))
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("%"):
            declared = stripped[1:].strip()
            if declared and declared != fmt:
                diags.append(Diagnostic(
                    "warning", "FORMAT_HEADER", "", idx,
                    f"格式头 {declared!r} 非 {fmt}，仍按 {fmt} 解析", stripped))
            if declared:
                fmt = declared
            continue
        if stripped in SECTION_SET:
            current = stripped[1:]
            continue
        if stripped.startswith("@"):
            diags.append(Diagnostic(
                "warning", "UNKNOWN_SECTION", stripped, idx,
                f"未知段 {stripped}，原始行保留但不参与计算", stripped))
            current = "_unknown:" + stripped
            rows.setdefault(current, [])
            continue

        row = Row(lineno=idx, raw_line=line, tokens=stripped.split())
        try:
            _fill_row(current, row, diags, meta)
        except Exception as exc:  # 防御：异常转为诊断而非崩溃
            diags.append(Diagnostic("error", "INTERNAL", current, idx, repr(exc)))
        rows.setdefault(current, []).append(row)

    _cross_checks(rows, diags)
    source_hash = "src_" + digest({"raw_text": raw_text})
    return ParsedDocument(fmt, raw_text, meta, rows, diags, source_hash)


def _fill_row(section: str, row: Row, diags: List[Diagnostic],
              meta: Dict[str, str]) -> None:
    tok = row.tokens
    ln = row.lineno
    if section.startswith("_unknown:"):
        return
    if section == "meta":
        if "=" in tok[0]:
            key, _, val = tok[0].partition("=")
            rest = " ".join(tok[1:])
            val = (val + " " + rest).strip()
        elif len(tok) >= 2:
            key, val = tok[0], " ".join(tok[1:])
        else:
            diags.append(Diagnostic("error", "META_KEYVALUE", section, ln,
                                    "meta 需要 key=value", tok[0]))
            return
        row.data["key"] = key
        row.data["value_raw"] = val
        meta[key] = val
    elif section == "skeleton":
        _fill_skeleton(tok, row, diags, ln)
    elif section == "predict":
        _fill_predict(tok, row, diags, ln)
    elif section in ("1H", "13C"):
        _fill_peak(tok, row, diags, section, ln)
    elif section in EDGE_KINDS:
        _fill_edge(tok, row, diags, section, ln)
    else:
        diags.append(Diagnostic("error", "ROW_OUTSIDE_SECTION", "(none)", ln,
                                "数据行出现在任何已知段之前"))


def _fill_skeleton(tok: List[str], row: Row, diags: List[Diagnostic], ln: int) -> None:
    if len(tok) < 3:
        diags.append(Diagnostic("error", "SKELETON_FIELDS", "skeleton", ln,
                                "需要 atom element equivalence [bond ...]"))
        return
    aid, elem, equiv = tok[0], tok[1], tok[2]
    bonds = tok[3:]
    if not elem[:1].isalpha():
        diags.append(Diagnostic("error", "BAD_ELEMENT", "skeleton", ln,
                                f"元素符号非法: {elem}", elem))
    eq = _num(equiv)
    if eq is None or int(eq) != eq or int(eq) < 1:
        diags.append(Diagnostic("error", "BAD_EQUIV", "skeleton", ln,
                                f"对称多重度需为正整数: {equiv}", equiv))
    row.data = {"atom": aid, "element": elem,
                "equivalence_raw": equiv, "equivalence": int(eq) if eq else None,
                "bonds_raw": bonds}


def _fill_predict(tok: List[str], row: Row, diags: List[Diagnostic], ln: int) -> None:
    if len(tok) < 3:
        diags.append(Diagnostic("error", "PREDICT_FIELDS", "predict", ln,
                                "需要 nucleus kind shift_ppm [multiplicity]"))
        return
    nucleus, kind, shift_tok = tok[0], tok[1], tok[2]
    if kind not in ("1H", "13C"):
        diags.append(Diagnostic("error", "BAD_PREDICT_KIND", "predict", ln,
                                f"核种类需为 1H/13C: {kind}", kind))
    s = _shift(shift_tok)
    if s["value"] is None:
        diags.append(Diagnostic("error", "BAD_SHIFT", "predict", ln,
                                f"预测化学位移无法转换: {shift_tok}", shift_tok))
    mult = tok[3] if len(tok) > 3 else None
    if mult is not None and mult not in MULTIPLICITIES:
        diags.append(Diagnostic("warning", "UNKNOWN_MULTIPLICITY", "predict", ln,
                                f"未知多重性记号: {mult}", mult))
    row.data = {"nucleus": nucleus, "kind": kind, "shift": s,
                "multiplicity_raw": mult, "multiplicity": mult}


def _fill_peak(tok: List[str], row: Row, diags: List[Diagnostic],
               section: str, ln: int) -> None:
    if len(tok) < 3:
        diags.append(Diagnostic("error", "PEAK_FIELDS", section, ln,
                                "需要 shift intensity integral [multiplicity]"))
        return
    s = _shift(tok[0])
    if s["value"] is None:
        diags.append(Diagnostic("error", "BAD_SHIFT", section, ln,
                                f"化学位移无法转换: {tok[0]}", tok[0]))
    intensity_raw, integral_raw = tok[1], tok[2]
    intensity = _num(intensity_raw)
    integral = _num(integral_raw)
    if intensity is None:
        diags.append(Diagnostic("warning", "BAD_INTENSITY", section, ln,
                                f"峰强无法转换，原值保留: {intensity_raw}",
                                intensity_raw))
    if integral is None:
        diags.append(Diagnostic("warning", "BAD_INTEGRAL", section, ln,
                                f"积分无法转换，原值保留: {integral_raw}",
                                integral_raw))
    mult = tok[3] if len(tok) > 3 else None
    if mult is not None and mult not in MULTIPLICITIES:
        diags.append(Diagnostic("warning", "UNKNOWN_MULTIPLICITY", section, ln,
                                f"未知多重性记号: {mult}", mult))
    row.data = {"shift": s,
                "intensity_raw": intensity_raw, "intensity": intensity,
                "intensity_unit": "arbitrary",
                "integral_raw": integral_raw, "integral": integral,
                "integral_unit": "proton_count" if section == "1H" else "carbon_count",
                "multiplicity_raw": mult, "multiplicity": mult}


def _fill_edge(tok: List[str], row: Row, diags: List[Diagnostic],
               section: str, ln: int) -> None:
    if len(tok) < 2:
        diags.append(Diagnostic("error", "EDGE_FIELDS", section, ln,
                                "需要 peak_ref_a peak_ref_b [delta_ppm]"))
        return
    a, b = tok[0], tok[1]
    delta = tok[2] if len(tok) > 2 else None
    delta_pairs: List[Dict[str, Any]] = []
    if delta is not None:
        for part in delta.split(","):
            ds = _shift(part)
            if ds["value"] is None:
                diags.append(Diagnostic("warning", "BAD_EDGE_DELTA", section, ln,
                                        f"相关位移无法转换: {part}", part))
            delta_pairs.append(ds)
    if a == b:
        diags.append(Diagnostic("warning", "SELF_EDGE", section, ln,
                                f"{section} 自相关边 {a}~{b}", a))
    row.data = {"kind": section, "a_raw": a, "b_raw": b,
                "a_refs": _expand_ref(a), "b_refs": _expand_ref(b),
                "delta": delta, "delta_shifts": delta_pairs}


def _expand_ref(tok: str) -> List[str]:
    """峰引用支持 'p1' / 重叠组 '{p1|p2}'；解析阶段不强制唯一。"""
    if tok.startswith("{") and tok.endswith("}"):
        return [p for p in tok[1:-1].split("|") if p]
    return [tok]


def _cross_checks(rows: Dict[str, List[Row]], diags: List[Diagnostic]) -> None:
    # 骨架键引用必须指向已声明原子
    declared = {r.data.get("atom") for r in rows.get("skeleton", []) if r.data}
    for r in rows.get("skeleton", []):
        for b in r.data.get("bonds_raw", []):
            if b not in declared:
                diags.append(Diagnostic("error", "UNKNOWN_ATOM_REF", "skeleton",
                                        r.lineno, f"键引用未声明原子 {b}", b))
    for r in rows.get("predict", []):
        nuc = r.data.get("nucleus")
        if nuc is not None and nuc not in declared:
            diags.append(Diagnostic("error", "PREDICT_ATOM_REF", "predict",
                                    r.lineno, f"预测核未在骨架声明: {nuc}", nuc))
    # 同段峰行不允许稳定键重复（防止误提交）
    for sec in ("1H", "13C"):
        seen = set()
        for r in rows.get(sec, []):
            v = r.data.get("shift", {}).get("value")
            key = round(v, 6) if isinstance(v, (int, float)) else r.data.get("shift", {}).get("raw")
            if key in seen:
                diags.append(Diagnostic("warning", "DUPLICATE_SHIFT", sec,
                                        r.lineno, f"重复化学位移行（重叠峰应使用各自行）: {key}"))
            seen.add(key)
