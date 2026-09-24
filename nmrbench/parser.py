"""可复制的小型 NMR 文本格式解析器。

peak list 与骨架均为行式文本。解析器严格保留原始字符串：
- 每条记录带 raw（原始行）
- 文档级 diagnostics 收集格式错误；无法转换的字段留在原位
解析不抛异常退出，错误以诊断形式返回，供导入层明确呈现。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import units as U


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ParseResult:
    doc: dict
    diagnostics: list = field(default_factory=list)


def _kv(line: str) -> tuple[str, str] | None:
    if ":" not in line:
        return None
    key, val = line.split(":", 1)
    return key.strip().lower(), val.strip()


def _fields(tokens: list[str], raw: str, diag: list, record_id: str) -> dict:
    out: dict[str, str] = {}
    for tok in tokens:
        if "=" not in tok:
            diag.append({"level": "error", "record": record_id,
                         "message": f"field without '=': '{tok}'", "raw": raw})
            continue
        k, v = tok.split("=", 1)
        if k in out:
            diag.append({"level": "warning", "record": record_id,
                         "message": f"duplicate field '{k}' keeps first value", "raw": raw})
            continue
        out[k] = v
    return out


def parse_peak_list(text: str) -> ParseResult:
    """解析一维/二维混合 peak list。

    头部键: kind, compound, solvent, temperature, reference, frequency。
    peaks: 段每行: <id> shift=<x> intensity=<x> integration=<x> multiplicity=<tok>[j=<x>]
    edges: 段每行: <id> type=<COSY|HSQC|HMBC> from=<id|@shiftppm> to=<id|@shiftppm>
    """
    diag: list = []
    lines = text.splitlines()
    if not lines or not lines[0].strip().startswith("nmr-list"):
        diag.append({"level": "error", "record": "header",
                     "message": "first line must be 'nmr-list 1'", "raw": lines[0] if lines else ""})
    header: dict = {}
    peaks: list = []
    edges: list = []
    section = None
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("peaks:", "edges:"):
            section = stripped[:-1]
            continue
        if section is None:
            pair = _kv(line)
            if pair is None:
                diag.append({"level": "error", "record": "header",
                             "message": f"malformed header line (need key: value): '{stripped}'"})
                continue
            key, val = pair
            if key in header:
                diag.append({"level": "warning", "record": "header",
                             "message": f"duplicate header '{key}' keeps first value"})
                continue
            header[key] = val
            continue
        tokens = stripped.split()
        record_id = tokens[0] if tokens else "?"
        f = _fields(tokens[1:], stripped, diag, record_id)
        if section == "peaks":
            peak = {"id": record_id, "raw": stripped,
                    "shift": None, "intensity": None, "integration": None,
                    "multiplicity": None, "multiplicity_raw": None,
                    "coupling": None}
            if "shift" not in f:
                diag.append({"level": "error", "record": record_id,
                             "message": "peak missing shift", "raw": stripped})
            else:
                m = U.parse_shift(f["shift"])
                peak["shift"] = m.as_dict()
                if m.unconvertible:
                    diag.append({"level": "error", "record": record_id,
                                 "message": f"unconvertible shift: {m.diagnostic}",
                                 "raw": stripped})
            if "intensity" in f:
                m = U.parse_intensity(f["intensity"])
                peak["intensity"] = m.as_dict()
                if m.unconvertible:
                    diag.append({"level": "error", "record": record_id,
                                 "message": f"unconvertible intensity: {m.diagnostic}",
                                 "raw": stripped})
            if "integration" in f:
                m = U.parse_intensity(f["integration"])
                peak["integration"] = m.as_dict()
                if m.unconvertible:
                    diag.append({"level": "error", "record": record_id,
                                 "message": f"unconvertible integration: {m.diagnostic}",
                                 "raw": stripped})
            if "multiplicity" in f:
                peak["multiplicity_raw"] = f["multiplicity"]
                peak["multiplicity"] = _normalize_multiplicity(f["multiplicity"])
            if "j" in f:
                m = U.parse_coupling(f["j"])
                peak["coupling"] = m.as_dict()
                if m.unconvertible:
                    diag.append({"level": "error", "record": record_id,
                                 "message": f"unconvertible coupling: {m.diagnostic}",
                                 "raw": stripped})
            peaks.append(peak)
        elif section == "edges":
            etype = f.get("type", "").upper()
            if etype not in {"COSY", "HSQC", "HMBC"}:
                diag.append({"level": "error", "record": record_id,
                             "message": f"edge type must be COSY/HSQC/HMBC, got '{etype}'",
                             "raw": stripped})
            endpoints = {}
            for side in ("from", "to"):
                raw_ep = f.get(side)
                if raw_ep is None:
                    diag.append({"level": "error", "record": record_id,
                                 "message": f"edge missing '{side}'", "raw": stripped})
                    endpoints[side] = {"kind": "missing", "raw": None}
                elif raw_ep.startswith("@"):
                    m = U.parse_shift(raw_ep[1:])
                    endpoints[side] = {"kind": "coordinate", "raw": raw_ep,
                                       "shift": m.as_dict()}
                    if m.unconvertible:
                        diag.append({"level": "error", "record": record_id,
                                     "message": f"unconvertible coordinate endpoint: {m.diagnostic}",
                                     "raw": stripped})
                else:
                    endpoints[side] = {"kind": "peak", "raw": raw_ep, "peak_id": raw_ep}
            edges.append({"id": record_id, "type": etype, "raw": stripped,
                          "from": endpoints["from"], "to": endpoints["to"]})
    # 解析头部度量
    meta = {"compound": header.get("compound"), "solvent": header.get("solvent"),
            "reference": header.get("reference", "TMS"),
            "temperature_raw": header.get("temperature"),
            "frequency_raw": header.get("frequency"),
            "temperature": None, "frequency": None, "kind": None}
    if "kind" in header:
        try:
            meta["kind"] = U.parse_nucleus(header["kind"])
        except U.UnitError as exc:
            diag.append({"level": "error", "record": "header",
                         "message": str(exc), "raw": header["kind"]})
    else:
        diag.append({"level": "error", "record": "header",
                     "message": "missing 'kind:' header"})
    if "frequency" in header:
        m = U.parse_frequency(header["frequency"])
        meta["frequency"] = m.as_dict()
        if m.unconvertible:
            diag.append({"level": "error", "record": "header",
                         "message": f"unconvertible frequency: {m.diagnostic}",
                         "raw": header["frequency"]})
    else:
        diag.append({"level": "error", "record": "header",
                     "message": "missing 'frequency:' header"})
    if "temperature" in header:
        m = U.parse_temperature(header["temperature"])
        meta["temperature"] = m.as_dict()
        if m.unconvertible:
            diag.append({"level": "error", "record": "header",
                         "message": f"unconvertible temperature: {m.diagnostic}",
                         "raw": header["temperature"]})
    if "solvent" not in header:
        diag.append({"level": "warning", "record": "header",
                     "message": "missing 'solvent:' header"})
    # 端点引用完整性
    peak_ids = {p["id"] for p in peaks}
    for e in edges:
        for side in ("from", "to"):
            ep = e[side]
            if ep["kind"] == "peak" and ep["peak_id"] not in peak_ids:
                diag.append({"level": "error", "record": e["id"],
                             "message": f"edge endpoint '{ep['peak_id']}' does not match any peak",
                             "raw": e["raw"]})
    doc = {"schema": "nmr-list", "header": meta, "peaks": peaks, "edges": edges,
           "content_hash": content_hash(text)}
    return ParseResult(doc, diag)


_MULT_MAP = {
    "s": "singlet", "singlet": "singlet",
    "d": "doublet", "doublet": "doublet",
    "t": "triplet", "triplet": "triplet",
    "q": "quartet", "quartet": "quartet",
    "quintet": "quintet", "sextet": "sextet", "septet": "septet",
    "m": "multiplet", "multiplet": "multiplet",
    "brs": "broad-singlet",
}


def _normalize_multiplicity(token: str) -> str | None:
    base = token.split("(", 1)[0].strip().lower()
    return _MULT_MAP.get(base)


def parse_skeleton(text: str) -> ParseResult:
    """解析分子骨架（原子、键、等价组、布局坐标）。"""
    diag: list = []
    lines = text.splitlines()
    if not lines or not lines[0].strip().startswith("skeleton"):
        diag.append({"level": "error", "record": "header",
                     "message": "first line must be 'skeleton 1'",
                     "raw": lines[0] if lines else ""})
    header: dict = {}
    atoms: list = []
    bonds: list = []
    equiv: list = []
    layout: dict = {}
    section = None
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("atoms:", "bonds:", "equiv:", "layout:"):
            section = stripped[:-1]
            continue
        if section is None:
            pair = _kv(line)
            if pair is None:
                diag.append({"level": "error", "record": "header",
                             "message": f"malformed header line: '{stripped}'"})
                continue
            header[pair[0]] = pair[1]
            continue
        if section == "atoms":
            tokens = stripped.split()
            atom_id = tokens[0]
            f = _fields(tokens[1:], stripped, diag, atom_id)
            try:
                kind = U.parse_nucleus(f.get("kind", ""))
            except U.UnitError as exc:
                diag.append({"level": "error", "record": atom_id,
                             "message": str(exc), "raw": stripped})
                kind = None
            atom = {"id": atom_id, "raw": stripped, "kind": kind,
                    "shift": None, "shift_raw": f.get("shift"),
                    "multiplicity": _normalize_multiplicity(f["multiplicity"])
                    if "multiplicity" in f else None,
                    "multiplicity_raw": f.get("multiplicity"),
                    "integration": f.get("integration"),
                    "attached_to": f.get("attached_to")}
            if "shift" in f:
                m = U.parse_shift(f["shift"])
                atom["shift"] = m.as_dict()
                if m.unconvertible:
                    diag.append({"level": "error", "record": atom_id,
                                 "message": f"unconvertible predicted shift: {m.diagnostic}",
                                 "raw": stripped})
            else:
                diag.append({"level": "error", "record": atom_id,
                             "message": "atom missing predicted shift", "raw": stripped})
            atoms.append(atom)
        elif section == "bonds":
            token = stripped.split()[0]
            if "-" not in token:
                diag.append({"level": "error", "record": token,
                             "message": "bond must look like A-B", "raw": stripped})
                continue
            a, b = token.split("-", 1)
            bonds.append({"id": token, "a": a.strip(), "b": b.strip(), "raw": stripped})
        elif section == "equiv":
            token = stripped.split()[0]
            group = [x.strip() for x in token.split("=") if x.strip()]
            if len(group) < 2:
                diag.append({"level": "error", "record": token,
                             "message": "equiv group needs at least two atoms", "raw": stripped})
            equiv.append(group)
        elif section == "layout":
            tokens = stripped.split()
            atom_id = tokens[0]
            f = _fields(tokens[1:], stripped, diag, atom_id)
            try:
                layout[atom_id] = {"x": float(f["x"]), "y": float(f["y"])}
            except (KeyError, ValueError):
                diag.append({"level": "error", "record": atom_id,
                             "message": "layout needs numeric x=<n> y=<n>", "raw": stripped})
    atom_ids = {a["id"] for a in atoms}
    for bond in bonds:
        for endpoint in (bond["a"], bond["b"]):
            if endpoint not in atom_ids:
                diag.append({"level": "error", "record": bond["id"],
                             "message": f"bond references unknown atom '{endpoint}'",
                             "raw": bond["raw"]})
    for atom in atoms:
        if atom["attached_to"] and atom["attached_to"] not in atom_ids:
            diag.append({"level": "error", "record": atom["id"],
                         "message": f"attached_to unknown atom '{atom['attached_to']}'",
                         "raw": atom["raw"]})
    for group in equiv:
        for atom_id in group:
            if atom_id not in atom_ids:
                diag.append({"level": "error", "record": "=".join(group),
                             "message": f"equiv references unknown atom '{atom_id}'"})
    doc = {"schema": "skeleton", "compound": header.get("compound"),
           "name": header.get("name"), "atoms": atoms, "bonds": bonds,
           "equiv": equiv, "layout": layout, "content_hash": content_hash(text)}
    return ParseResult(doc, diag)
