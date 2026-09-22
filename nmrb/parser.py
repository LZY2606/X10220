"""导入层：解析领域化的小型 NMR 文本格式。

格式说明（示例见 ``nmrb/case`` 包）：

``*.pk1`` 一维峰表（每个文件一种核，条件在文件头）::

    @format NMRB-PEAKS1/1
    #nucleus 1H
    #solvent CDCl3
    #temperature-k 298.15
    #spectrometer-mhz 500.13
    #reference TMS@0ppm
    # 行标签  化学位移(ppm)  峰强(任意单位)  积分(核数)  多重性
    @p1  7.30  100  2.0  d,J=8.0Hz
    @p2  xx    12   0.5  br

``*.peak2`` 二维相关边::

    @format NMRB-PEAKS2/1
    #experiment COSY            # COSY | HSQC | HMBC
    #nuclei 1H,1H
    @e1  7.30  2.61  weak

``*.skel`` 分子骨架与预期位点::

    @format NMRB-SKELETON/1
    #compound Exemplin-A
    @s1 C1 H_A aromatic 7.25ppm group=G_A
    @s2 C2 H_B aromatic 7.25ppm group=G_B
    @bond H_A H_H vicinal          # 预期相邻（COSY 证据）
    @hmbc H_D C_A                   # 预期远程（HMBC 证据）

设计原则：
- 保留原始行文本、原始标记与单位；派生规范化在领域层进行。
- 无法转换的值留在原位置并产生诊断，绝不静默丢弃。
- 解析是纯函数，不触碰数据库。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_FORMATS = {"NMRB-PEAKS1/1", "NMRB-PEAKS2/1", "NMRB-SKELETON/1"}
_EXPERIMENTS = {"COSY", "HSQC", "HMBC"}
_MULTIPLICITY = {"s", "d", "t", "q", "p", "h", "hept", "m", "br", "dd", "dt", "td", "triplet", "doublet", "br", "sext", "vt"}


@dataclass
class Diagnostic:
    level: str          # error | warning
    code: str
    lineno: int
    message: str
    raw: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "code": self.code, "lineno": self.lineno,
                "message": self.message, "raw": self.raw}


@dataclass
class RawRecord:
    label: Optional[str]
    lineno: int
    kind: str
    raw: str
    fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    logical_name: str
    format: str
    raw_text: str = "" 
    headers: Dict[str, str] = field(default_factory=dict)
    records: List[RawRecord] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def has_error(self) -> bool:
        return any(d.level == "error" for d in self.diagnostics)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "logical_name": self.logical_name,
            "format": self.format,
            "headers": self.headers,
            "records": [
                {"label": r.label, "lineno": r.lineno, "kind": r.kind,
                 "raw": r.raw, "fields": r.fields}
                for r in self.records
            ],
            "diagnostics": [d.as_dict() for d in self.diagnostics],
            "has_error": self.has_error,
        }


def _split_tokens(line: str) -> List[str]:
    return [t for t in re.split(r"[\s,]+", line.strip()) if t]


def _parse_number_with_unit(token: str) -> Tuple[Optional[float], str, Optional[str]]:
    """返回 (数值, 原始表示, 单位)。无法转换时数值为 None。"""
    m = re.fullmatch(r"([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)([A-Za-z%°@./0-9]*)", token)
    if not m:
        return None, token, None
    value = float(m.group(1))
    unit = m.group(2) or None
    return value, token, unit


def parse_text(logical_name: str, text: str) -> ParsedDocument:
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        return ParsedDocument(logical_name, "?", diagnostics=[
            Diagnostic("error", "EMPTY_FILE", 0, "空文件：缺少 @format 声明行")])

    first = lines[0].strip()
    m = re.match(r"@format\s+(\S+)", first)
    if not m:
        return ParsedDocument(logical_name, "?", diagnostics=[
            Diagnostic("error", "NO_FORMAT_HEADER", 1, "首行必须是 '@format <格式>'", first)])
    fmt = m.group(1)
    if fmt not in _FORMATS:
        return ParsedDocument(logical_name, fmt, diagnostics=[
            Diagnostic("error", "UNKNOWN_FORMAT", 1, f"未知格式 {fmt}；支持 {sorted(_FORMATS)}", first)])

    doc = ParsedDocument(logical_name, fmt, raw_text=text)
    for idx, raw in enumerate(lines[1:], start=2):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line[1:].strip()
            if not body:
                continue
            key, _, value = body.partition(" ")
            doc.headers[key.strip().lower()] = value.strip()
            continue
        if fmt == "NMRB-PEAKS1/1":
            _parse_peak1(doc, idx, line, raw)
        elif fmt == "NMRB-PEAKS2/1":
            _parse_peak2(doc, idx, line, raw)
        else:
            _parse_skeleton(doc, idx, line, raw)
    _validate(doc)
    return doc


def _label_and_rest(line: str) -> Tuple[Optional[str], str]:
    m = re.match(r"(@[A-Za-z0-9_]+)\s+(.*)", line)
    if m:
        return m.group(1), m.group(2)
    return None, line


def _parse_peak1(doc: ParsedDocument, lineno: int, line: str, raw: str) -> None:
    label, rest = _label_and_rest(line)
    toks = _split_tokens(rest)
    if len(toks) < 4:
        doc.diagnostics.append(Diagnostic("error", "BAD_COLUMN_COUNT", lineno,
                                          f"一维峰至少需要 4 列（位移/峰强/积分/多重性），实际 {len(toks)}", raw))
        return
    shift_tok, intensity_tok, integral_tok, multiplicity = toks[0], toks[1], toks[2], " ".join(toks[3:])
    shift, shift_raw, shift_unit = _parse_number_with_unit(shift_tok)
    intensity, intensity_raw, intensity_unit = _parse_number_with_unit(intensity_tok)
    integral, integral_raw, integral_unit = _parse_number_with_unit(integral_tok)
    fields: Dict[str, Any] = {
        "shift_ppm": shift, "shift_repr": shift_raw, "shift_unit": shift_unit or "ppm",
        "intensity": intensity, "intensity_repr": intensity_raw, "intensity_unit": intensity_unit or "counts",
        "integral": integral, "integral_repr": integral_raw, "integral_unit": integral_unit or "H",
        "multiplicity": multiplicity,
    }
    rec = RawRecord(label, lineno, "peak1d", raw, fields)
    doc.records.append(rec)
    if shift is None:
        doc.diagnostics.append(Diagnostic("error", "UNCONVERTIBLE_SHIFT", lineno,
                                          f"化学位移无法转换：{shift_tok!r}；原值保留在记录中，不参与规范化", raw))
    if intensity is None:
        doc.diagnostics.append(Diagnostic("warning", "UNCONVERTIBLE_INTENSITY", lineno,
                                          f"峰强无法转换：{intensity_tok!r}；原值保留，仅从积分/位移取证", raw))
    if integral is None:
        doc.diagnostics.append(Diagnostic("error", "UNCONVERTIBLE_INTEGRAL", lineno,
                                          f"积分无法转换：{integral_tok!r}；原值保留在记录中", raw))
    mult_head = re.split(r",|\s", multiplicity, maxsplit=1)[0].lower()
    if mult_head not in _MULTIPLICITY:
        doc.diagnostics.append(Diagnostic("warning", "UNKNOWN_MULTIPLICITY", lineno,
                                          f"未识别多重性 {mult_head!r}，原样保留", raw))


def _parse_peak2(doc: ParsedDocument, lineno: int, line: str, raw: str) -> None:
    label, rest = _label_and_rest(line)
    toks = _split_tokens(rest)
    if len(toks) < 3:
        doc.diagnostics.append(Diagnostic("error", "BAD_COLUMN_COUNT", lineno,
                                          f"二维边至少需要 3 列（f1/f2/强度），实际 {len(toks)}", raw))
        return
    f1, f1_raw, f1_unit = _parse_number_with_unit(toks[0])
    f2, f2_raw, f2_unit = _parse_number_with_unit(toks[1])
    strength = toks[2].lower()
    fields: Dict[str, Any] = {
        "f1_ppm": f1, "f1_repr": f1_raw, "f1_unit": f1_unit or "ppm",
        "f2_ppm": f2, "f2_repr": f2_raw, "f2_unit": f2_unit or "ppm",
        "strength": strength, "strength_repr": toks[2],
    }
    doc.records.append(RawRecord(label, lineno, "edge2d", raw, fields))
    if f1 is None or f2 is None:
        doc.diagnostics.append(Diagnostic("error", "UNCONVERTIBLE_COORD", lineno,
                                          f"二维坐标无法转换：{toks[0]!r},{toks[1]!r}；原值保留", raw))
    if strength not in {"strong", "medium", "weak"}:
        doc.diagnostics.append(Diagnostic("warning", "UNKNOWN_STRENGTH", lineno,
                                          f"未识别相关强度 {strength!r}，原样保留", raw))


def _parse_skeleton(doc: ParsedDocument, lineno: int, line: str, raw: str) -> None:
    label, rest = _label_and_rest(line)
    head = rest.split(None, 1)[0] if rest.split() else ""
    if head == "bond" or head == "hmbc":
        toks = _split_tokens(rest)
        if len(toks) < 3:
            doc.diagnostics.append(Diagnostic("error", "BAD_EDGE", lineno,
                                              f"{head} 需要两个端点与类型", raw))
            return
        doc.records.append(RawRecord(label, lineno, head, raw,
                                     {"from": toks[1], "to": toks[2],
                                      "kind": toks[3] if len(toks) > 3 else ""}))
        return
    toks = _split_tokens(rest)
    if len(toks) < 4:
        doc.diagnostics.append(Diagnostic("error", "BAD_SITE", lineno,
                                          f"位点至少需要 碳/氢/元素/位移 4 列", raw))
        return
    carbon, hydrogen, element, shift_tok = toks[0], toks[1], toks[2], toks[3]
    shift, shift_repr, shift_unit = _parse_number_with_unit(shift_tok)
    group = ""
    m = re.search(r"group=(\S+)", rest)
    if m:
        group = m.group(1)
    protons = 1
    mp = re.search(r"protons=(\d+)", rest)
    if mp:
        protons = int(mp.group(1))
    doc.records.append(RawRecord(label, lineno, "site", raw, {
        "carbon": carbon, "hydrogen": hydrogen, "element": element,
        "shift_ppm": shift, "shift_repr": shift_repr, "shift_unit": shift_unit or "ppm",
        "group": group,
        "protons": protons,
    }))
    if shift is None:
        doc.diagnostics.append(Diagnostic("error", "UNCONVERTIBLE_SHIFT", lineno,
                                          f"位点预期位移无法转换：{shift_tok!r}", raw))


def _validate(doc: ParsedDocument) -> None:
    if doc.format == "NMRB-PEAKS2/1":
        exp = doc.headers.get("experiment", "").upper()
        if exp not in _EXPERIMENTS:
            doc.diagnostics.append(Diagnostic("error", "BAD_EXPERIMENT", 0,
                                              f"#experiment 必须是 {sorted(_EXPERIMENTS)}，得到 {exp!r}"))
    if doc.format == "NMRB-PEAKS1/1":
        if "nucleus" not in doc.headers:
            doc.diagnostics.append(Diagnostic("error", "MISSING_HEADER", 0,
                                              "一维峰表缺少 #nucleus 声明"))
        for key in ("solvent", "spectrometer-mhz"):
            if key not in doc.headers:
                doc.diagnostics.append(Diagnostic("warning", "MISSING_HEADER", 0,
                                                  f"建议声明 #{key}，原值缺失已留诊断"))
    labels = [r.label for r in doc.records if r.label]
    dup = {x for x in labels if labels.count(x) > 1}
    for name in sorted(dup):
        doc.diagnostics.append(Diagnostic("error", "DUPLICATE_LABEL", 0,
                                          f"行标签重复：{name}"))
