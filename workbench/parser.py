"""导入层：解析一维/二维 NMR peak list 小格式。

设计原则（见任务书）：
- 原始文本原样保留，解析结果中每个量同时给出 raw 表示、value 规范化值与 unit/basis；
- 无法转换的行留在原位（records）并附 diagnostics，不丢弃、不画图时不静默吞掉；
- 文件级错误（缺 DATA 段、未知谱型 token、缺必填头）抛 ParseError；
- 行级错误收集在 diagnostics，整份文件仍入库（原始输入不可覆盖）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ids import source_id

TOKENS = {
    "H1_PEAKS": "1d",
    "C13_PEAKS": "1d",
    "COSY": "2d",
    "HSQC": "2d",
    "HMBC": "2d",
}

HEADER_FLOAT = {"frequency_mhz", "temperature_k", "calibration_hz"}
HEADER_TEXT = {"spectrum", "solvent", "reference", "version_note"}


class ParseError(Exception):
    """文件级硬格式错误：无法作为该谱型的 peak list 入库。"""


@dataclass
class ParsedSource:
    source_id: str
    filename: str
    version: int
    spectrum: str
    kind: str  # 1d | 2d
    raw_text: str
    header: dict
    records: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)


def _make_diag(level: str, code: str, message: str, line: int | None = None, raw: str | None = None) -> dict:
    return {"level": level, "code": code, "message": message, "line": line, "raw": raw}


def _parse_header(tokens: list[str]) -> dict:
    header: dict = {}
    for raw in tokens[1:]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ParseError(f"文件头格式错误，应为 key=value：{line!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in HEADER_FLOAT:
            try:
                header[key] = {"raw": value, "value": float(value), "unit": _unit_for(key)}
            except ValueError:
                raise ParseError(f"文件头 {key} 不是数值：{value!r}")
        elif key in HEADER_TEXT:
            header[key] = {"raw": value, "value": value}
        else:
            raise ParseError(f"未知文件头字段：{key!r}")
    if "spectrum" not in header:
        raise ParseError("文件头缺少 spectrum=...，谱型与 token 不一致")
    if header["spectrum"]["value"] != tokens[0]:
        raise ParseError(f"spectrum={header['spectrum']['value']} 与文件 token {tokens[0]} 不一致")
    return header


def _unit_for(key: str) -> str:
    return {"frequency_mhz": "MHz", "temperature_k": "K", "calibration_hz": "Hz"}.get(key, "")


def parse_source(filename: str, version: int, raw_text: str) -> ParsedSource:
    lines = raw_text.splitlines()
    token_line = next((ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")), None)
    if token_line is None:
        raise ParseError("空文件：缺少谱型 token（H1_PEAKS/C13_PEAKS/COSY/HSQC/HMBC）")
    token = token_line.split()[0]
    if token not in TOKENS:
        raise ParseError(f"未知谱型 token：{token!r}")
    if "DATA" not in raw_text.splitlines():
        raise ParseError(f"{token} 文件缺少 DATA 段")

    data_idx = lines.index("DATA")
    kind = TOKENS[token]
    header = _parse_header([token] + lines[: data_idx])
    parsed = ParsedSource(
        source_id=source_id(filename, version, raw_text),
        filename=filename,
        version=version,
        spectrum=token,
        kind=kind,
        raw_text=raw_text,
        header=header,
    )
    for lineno, raw in enumerate(lines[data_idx + 1:], start=data_idx + 2):
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        if kind == "1d":
            parsed.records.append(_parse_1d_row(token, body, lineno, parsed.diagnostics))
        else:
            parsed.records.append(_parse_2d_row(token, body, lineno, parsed.diagnostics))
    return parsed


def _qty(value: float, unit: str, basis: str = "spectrum_axis") -> dict:
    return {"value": round(value, 6), "unit": unit, "basis": basis}


def _parse_1d_row(spectrum: str, body: str, lineno: int, diags: list[dict]) -> dict:
    parts = body.split()
    nucleus = "1H" if spectrum == "H1_PEAKS" else "13C"
    shift_unit = "ppm"
    row = {
        "line": lineno,
        "raw": body,
        "nucleus": nucleus,
        "shift": None,
        "intensity": None,
        "integral": None,
        "multiplicity": None,
    }
    try:
        row["shift"] = {"raw": parts[0], **_qty(float(parts[0]), shift_unit,
                                                "chemical_shift_reported")}
    except (IndexError, ValueError):
        diags.append(_make_diag("error", "BAD_SHIFT", "化学位移无法解析为数值", lineno, body))
        return row
    if len(parts) >= 2:
        try:
            row["intensity"] = {"raw": parts[1], **_qty(float(parts[1]), "counts", "peak_intensity")}
        except ValueError:
            diags.append(_make_diag("error", "BAD_INTENSITY", "峰强无法解析为数值", lineno, body))
    else:
        diags.append(_make_diag("warning", "MISSING_INTENSITY", "缺少峰强列", lineno, body))
    if len(parts) >= 3:
        try:
            row["integral"] = {"raw": parts[2], **_qty(float(parts[2]), "H" if nucleus == "1H" else "C",
                                                       "reported_integral")}
        except ValueError:
            diags.append(_make_diag("error", "BAD_INTEGRAL", "积分无法解析为数值", lineno, body))
    else:
        diags.append(_make_diag("warning", "MISSING_INTEGRAL", "缺少积分列", lineno, body))
    if len(parts) >= 4:
        mult = parts[3]
        allowed = {"s", "d", "t", "q", "m", "dd", "dt", "td"}
        if mult not in allowed:
            diags.append(_make_diag("warning", "UNKNOWN_MULTIPLICITY",
                                    f"未识别的多重性 {mult!r}，按原表示保留", lineno, body))
        row["multiplicity"] = {"raw": mult, "value": mult, "controlled": mult in allowed}
    else:
        diags.append(_make_diag("warning", "MISSING_MULTIPLICITY", "缺少多重性列", lineno, body))
    if len(parts) > 4:
        diags.append(_make_diag("warning", "EXTRA_COLUMNS", f"多余列被忽略：{parts[4:]!r}", lineno, body))
    return row


def _parse_2d_row(spectrum: str, body: str, lineno: int, diags: list[dict]) -> dict:
    parts = body.split()
    edge = {
        "line": lineno,
        "raw": body,
        "experiment": spectrum,
        "h": None,
        "x": None,
        "x_nucleus": None,
        "intensity": None,
    }
    if len(parts) < 2:
        diags.append(_make_diag("error", "BAD_EDGE", "相关边至少需要两个化学位移坐标", lineno, body))
        return edge
    try:
        edge["h"] = {"raw": parts[0], **_qty(float(parts[0]), "ppm", "f2_1H_reported")}
    except ValueError:
        diags.append(_make_diag("error", "BAD_H_COORD", "F2(1H) 坐标无法解析", lineno, body))
    x_nucleus = "1H" if spectrum == "COSY" else "13C"
    edge["x_nucleus"] = x_nucleus
    try:
        edge["x"] = {"raw": parts[1], **_qty(float(parts[1]), "ppm",
                                             "f1_1H_reported" if x_nucleus == "1H" else "f1_13C_reported")}
    except ValueError:
        diags.append(_make_diag("error", "BAD_X_COORD", f"F1({x_nucleus}) 坐标无法解析", lineno, body))
    if len(parts) >= 3:
        try:
            edge["intensity"] = {"raw": parts[2], **_qty(float(parts[2]), "counts", "cross_peak_intensity")}
        except ValueError:
            diags.append(_make_diag("warning", "BAD_EDGE_INTENSITY", "相关峰强度无法解析，保留原值",
                                    lineno, body))
    if len(parts) > 3:
        diags.append(_make_diag("warning", "EXTRA_COLUMNS", f"多余列被忽略：{parts[3:]!r}", lineno, body))
    return edge


def parsed_to_dict(p: ParsedSource) -> dict:
    return {
        "source_id": p.source_id,
        "filename": p.filename,
        "version": p.version,
        "spectrum": p.spectrum,
        "kind": p.kind,
        "raw_text": p.raw_text,
        "header": p.header,
        "records": p.records,
        "diagnostics": p.diagnostics,
    }
