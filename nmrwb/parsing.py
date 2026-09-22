"""导入解析层。

原则：
- 原始文本一经提交不可覆盖，原样存入 inputs.raw_text；
- 解析产物（peaks/edges/params/atoms）同时保留原始表示（*_raw）与规范化值；
- 单个值无法转换时留在原位置并附诊断（diag），不丢弃；
- 结构性错误（缺表头、列数错误等）抛 ParseError，整个版本被拒绝。
"""
import json


class ParseError(Exception):
    pass


def _to_float(raw, diag, field):
    try:
        return float(raw)
    except (TypeError, ValueError):
        diag[field] = "unconvertible value kept raw: %r" % raw
        return None


def parse_peaks1d(raw):
    """格式：
    # nucleus: 1H
    id,shift_ppm,intensity,integral,multiplicity
    p1,7.20,0.81,2.0,m
    允许多个 nucleus 段（1H / 13C）。
    """
    peaks, nucleus, saw_header = [], None, False
    for ln, line in enumerate(raw.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        if s.startswith("#"):
            low = s[1:].strip().lower()
            if low.startswith("nucleus:"):
                val = low.split(":", 1)[1].strip()
                if val not in ("1h", "13c"):
                    raise ParseError("line %d: unknown nucleus %r" % (ln, val))
                nucleus = "1H" if val == "1h" else "13C"
            continue
        if s.lower().startswith("id,"):
            if nucleus is None:
                raise ParseError("line %d: column header before '# nucleus:'" % ln)
            saw_header = True
            continue
        if nucleus is None or not saw_header:
            raise ParseError("line %d: data row before nucleus/column header" % ln)
        parts = [p.strip() for p in s.split(",")]
        if len(parts) != 5:
            raise ParseError("line %d: expected 5 columns, got %d"
                             % (ln, len(parts)))
        lid, shift, inten, integ, mult = parts
        if not lid:
            raise ParseError("line %d: empty peak id" % ln)
        diag = {}
        peaks.append({
            "local_id": lid, "nucleus": nucleus,
            "shift_raw": shift, "shift_ppm": _to_float(shift, diag, "shift_ppm"),
            "intensity_raw": inten,
            "intensity": _to_float(inten, diag, "intensity"),
            "integral_raw": integ,
            "integral": _to_float(integ, diag, "integral"),
            "multiplicity": mult or None, "diag": diag,
        })
    if not peaks:
        raise ParseError("no peaks parsed")
    ids = [p["local_id"] for p in peaks]
    if len(set(ids)) != len(ids):
        raise ParseError("duplicate peak ids: %s" % ids)
    return peaks


_EDGE_KINDS = ("COSY", "HSQC", "HMBC")
_STRENGTH = {"strong": 1.0, "medium": 0.6, "weak": 0.3}


def parse_corr2d(raw):
    """格式：
    # kind: HSQC
    id,a,b,strength
    e1,p3,c5,strong
    """
    edges, kind, saw_header = [], None, False
    for ln, line in enumerate(raw.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        if s.startswith("#"):
            low = s[1:].strip().lower()
            if low.startswith("kind:"):
                val = low.split(":", 1)[1].strip().upper()
                if val not in _EDGE_KINDS:
                    raise ParseError("line %d: unknown correlation kind %r"
                                     % (ln, val))
                kind = val
            continue
        if s.lower().startswith("id,"):
            if kind is None:
                raise ParseError("line %d: column header before '# kind:'" % ln)
            saw_header = True
            continue
        if kind is None or not saw_header:
            raise ParseError("line %d: data row before kind/column header" % ln)
        parts = [p.strip() for p in s.split(",")]
        if len(parts) != 4:
            raise ParseError("line %d: expected 4 columns, got %d"
                             % (ln, len(parts)))
        lid, a, b, strength = parts
        diag = {}
        if strength.lower() in _STRENGTH:
            sval, sraw = _STRENGTH[strength.lower()], strength
        else:
            sval = _to_float(strength, diag, "strength")
            sraw = strength
        edges.append({"local_id": lid, "kind": kind, "a": a, "b": b,
                      "strength_raw": sraw, "strength": sval, "diag": diag})
    if not edges:
        raise ParseError("no correlation edges parsed")
    return edges


_PARAM_KEYS = ("solvent", "temperature_K", "reference", "freq_mhz")


def parse_params(raw):
    """格式：每行 'key: value'。必需 solvent/reference/freq_mhz。"""
    vals, diag = {}, {}
    for ln, line in enumerate(raw.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        if ":" not in s:
            raise ParseError("line %d: expected 'key: value'" % ln)
        key, val = [x.strip() for x in s.split(":", 1)]
        if key not in _PARAM_KEYS:
            diag[key] = "unknown key ignored"
            continue
        vals[key] = val
    for req in ("solvent", "reference", "freq_mhz"):
        if req not in vals:
            raise ParseError("missing required param: %s" % req)
    out = {"solvent": vals["solvent"], "reference": vals["reference"],
           "freq_raw": vals["freq_mhz"],
           "freq_mhz": _to_float(vals["freq_mhz"], diag, "freq_mhz"),
           "temperature_raw": vals.get("temperature_K"),
           "temperature_K": None, "diag": diag}
    if "temperature_K" in vals:
        out["temperature_K"] = _to_float(vals["temperature_K"], diag,
                                         "temperature_K")
    return out


def parse_molecule(raw):
    """JSON：{name, atoms:[{id,element,x,y,h_shift,c_shift,nh,mult,sym,attached}],
    bonds:[[a,b],...]}"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError("molecule is not valid JSON: %s" % exc)
    atoms = data.get("atoms")
    if not isinstance(atoms, list) or not atoms:
        raise ParseError("molecule.atoms must be a non-empty list")
    out = []
    for i, a in enumerate(atoms):
        if not a.get("id") or a.get("element") not in ("C", "H"):
            raise ParseError("atom %d: needs id and element C|H" % i)
        out.append({
            "atom_id": a["id"], "element": a["element"],
            "x": float(a.get("x", 0.0)), "y": float(a.get("y", 0.0)),
            "h_shift": a.get("h_shift"), "c_shift": a.get("c_shift"),
            "nh": a.get("nh"), "mult": a.get("mult"),
            "sym_group": a.get("sym"), "attached": a.get("attached"),
        })
    ids = {a["atom_id"] for a in out}
    if len(ids) != len(out):
        raise ParseError("duplicate atom ids")
    bonds = []
    for b in data.get("bonds", []):
        if len(b) != 2 or b[0] not in ids or b[1] not in ids:
            raise ParseError("bad bond: %r" % (b,))
        bonds.append((b[0], b[1]))
    return data.get("name", "molecule"), out, bonds
