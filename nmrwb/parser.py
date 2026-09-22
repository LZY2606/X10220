"""Import layer for NMR peak lists and molecular/rule documents.

Design rules:
  * the original text representation is retained verbatim;
  * raw inputs are never overwritten, revisions are new versions;
  * unconvertible values stay in place with a diagnostic instead of being
    silently normalised or dropped;
  * ordering of peaks, edges, atoms and bonds is preserved (`ord`).

Line-based grammar, ``#`` comments, blank lines ignored.

Spectrum::

    @spectrum / @kind 1d|cosy|hsqc|hmbc / @nucleus 1H|13C
    @compound / @solvent / @temperature 298K / @reference TMS
    @frequency-MHz 400.13 / @date / @operator
    1d : P1 shift=2.13ppm intensity=1.0 integral=3H mult=s J=7.0Hz note=...
    2d : E1 from=P1 to=P2 shift1=2.13ppm shift2=1.02ppm note=...

Structure::

    @structure / @atom C1 label=C element=C x=.. y=.. / @bond / @equiv g H5 H6

Rulepack::

    @rulepack / @param key=value / @nucleus 1H / @shift H1=2.10ppm
    / @condition solvent=CDCl3
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .ids import stable_id
from .models import Diagnostic, ParsedDocument
from . import models

_MULT_MAP = {
    "s": "s", "d": "d", "t": "t", "q": "q", "sext": "sext", "sept": "sept",
    "oct": "oct", "m": "m", "br": "br",
}
_NEIGH_COUNT = {"s": 0, "d": 1, "t": 2, "q": 3, "sext": 5, "sept": 6, "oct": 7}

HEADER_KEYS = (
    "kind", "nucleus", "compound", "solvent", "temperature", "reference",
    "frequency-MHz", "date", "operator", "version", "title",
)


def tokenize_kv(text: str) -> Dict[str, str]:
    """Parse ``key=value`` pairs; values may be quoted to keep spaces."""
    out: Dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        start = i
        while i < n and text[i] != "=" and not text[i].isspace():
            i += 1
        key = text[start:i]
        if i >= n or text[i] != "=":
            break
        i += 1
        if i < n and text[i] in ("'", '"'):
            quote = text[i]
            i += 1
            start = i
            while i < n and text[i] != quote:
                i += 1
            value = text[start:i]
            i += 1
        else:
            start = i
            while i < n and not text[i].isspace():
                i += 1
            value = text[start:i]
        if key:
            out[key] = value
    return out


def _split_value_unit(raw: str) -> Tuple[Optional[float], str]:
    num = raw.rstrip("ppmHzKk")
    try:
        return float(num), raw[len(num):]
    except ValueError:
        return None, raw


def parse_document(text: str, filename: str = "<input>") -> ParsedDocument:
    """Parse one source document. Structural problems are errors;
    unconvertible optional values are retained with a warning diagnostic."""
    header: Dict[str, str] = {}
    peaks: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    atoms: List[Dict[str, Any]] = []
    bonds: List[Dict[str, Any]] = []
    equiv: List[Dict[str, Any]] = []
    shifts: Dict[str, float] = {}
    params: Dict[str, str] = {}
    conditions: Dict[str, str] = {}
    diags: List[Diagnostic] = []
    doc_type = ""
    kind = ""
    nucleus: Optional[str] = None
    current_nucleus: Optional[str] = None
    seen_ids: Dict[str, int] = {}

    def err(code: str, msg: str, line: int, ref: Optional[str] = None) -> None:
        diags.append(Diagnostic("error", code, msg, line, ref))

    for lineno0, raw_line in enumerate(text.splitlines()):
        lineno = lineno0 + 1
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            parts = line.split(None, 1)
            key = parts[0][1:].lower()
            rest = parts[1].strip() if len(parts) > 1 else ""
            if key == "spectrum":
                doc_type, kind = "spectrum", ""
            elif key == "structure":
                doc_type, kind = "structure", "structure"
            elif key == "rulepack":
                doc_type, kind = "rulepack", "rulepack"
            elif key in ("kind", "nucleus"):
                header[key] = rest
                if key == "kind":
                    kind = rest
                if key == "nucleus":
                    nucleus = rest
                    current_nucleus = rest
            elif key == "condition":
                kv = tokenize_kv(rest)
                conditions.update(kv)
            elif key in ("atom", "bond", "equiv", "shift", "param"):
                if doc_type != "structure" and key in ("atom", "bond", "equiv"):
                    err("E-DIRECTIVE", "%s outside @structure" % key, lineno)
                elif doc_type != "rulepack" and key in ("shift", "param"):
                    err("E-DIRECTIVE", "%s outside @rulepack" % key, lineno)
                else:
                    _parse_directive(key, rest, lineno, atoms, bonds, equiv,
                                     shifts, params, diags, current_nucleus)
            else:
                canon = "frequency-MHz" if key == "frequency-mhz" else key
                header[canon] = rest
                if key in HEADER_KEYS and rest == "":
                    err("E-HEADER-EMPTY", "empty @%s value" % key, lineno)
            continue
        if ":" not in line:
            err("E-SYNTAX", "expected '<type> : <id> key=value ...'", lineno)
            continue
        type_part, body = line.split(":", 1)
        rtype = type_part.strip().lower()
        body = body.strip()
        rid, rest = _split_id(body)
        if not rid:
            err("E-SYNTAX", "missing record id", lineno)
            continue
        if rid in seen_ids:
            err("E-DUP-ID", "duplicate id %r (first on line %d)" %
                (rid, seen_ids[rid]), lineno, rid)
            continue
        seen_ids[rid] = lineno
        fields = tokenize_kv(rest)
        if rtype == "1d":
            if doc_type != "spectrum" or kind != "1d":
                err("E-RECORD-KIND", "1d peak outside a @kind 1d spectrum",
                    lineno, rid)
                continue
            peaks.append(_parse_peak(rid, fields, lineno, nucleus, diags))
        elif rtype == "2d":
            if doc_type != "spectrum" or kind not in models.TWO_D_KINDS:
                err("E-RECORD-KIND", "2d edge outside a 2d spectrum",
                    lineno, rid)
                continue
            edges.append(_parse_edge(rid, fields, lineno, kind, diags))
        else:
            err("E-RECORD-TYPE", "unknown record type %r" % rtype, lineno, rid)

    _validate(doc_type, kind, nucleus, header, peaks, edges, atoms, bonds,
              equiv, diags)

    if nucleus == "1H":
        kind = "1d"
    doc = ParsedDocument(doc_type, kind, nucleus, header, peaks, edges, atoms,
                         bonds, equiv, shifts, params, conditions, diags)
    return doc


def _split_id(body: str) -> Tuple[str, str]:
    parts = body.split(None, 1)
    if not parts:
        return "", ""
    rid = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    return rid, rest


def _number_field(rid: str, fields: Dict[str, str], key: str, lineno: int,
                  diags: List[Diagnostic], required: bool = True
                  ) -> Tuple[Optional[float], str]:
    if key not in fields:
        if required:
            diags.append(Diagnostic("error", "E-MISSING-FIELD",
                                    "%s: missing %s" % (rid, key), lineno, rid))
        return None, ""
    raw = fields[key]
    value, unit = _split_value_unit(raw)
    if value is None:
        diags.append(Diagnostic("warning", "W-UNCONVERTIBLE",
                                "%s: %s=%r kept as-is, not normalised"
                                % (rid, key, raw), lineno, rid))
        fields[key + "__unconvertible"] = "true"
    return value, unit


def _parse_peak(rid: str, fields: Dict[str, str], lineno: int,
                nucleus: Optional[str], diags: List[Diagnostic]
                ) -> Dict[str, Any]:
    shift, shift_unit = _number_field(rid, fields, "shift", lineno, diags)
    intensity_raw = fields.get("intensity")
    intensity = None
    if intensity_raw is not None:
        intensity, _ = _split_value_unit(intensity_raw)
        if intensity is None:
            diags.append(Diagnostic("warning", "W-UNCONVERTIBLE",
                                    "%s: intensity=%r kept as-is"
                                    % (rid, intensity_raw), lineno, rid))
    integral_raw = fields.get("integral")
    integral_num = None
    integral_unit = ""
    if integral_raw is not None:
        num = integral_raw.rstrip("H")
        integral_unit = integral_raw[len(num):]
        try:
            integral_num = float(num)
        except ValueError:
            diags.append(Diagnostic("warning", "W-UNCONVERTIBLE",
                                    "%s: integral=%r kept as-is"
                                    % (rid, integral_raw), lineno, rid))
            integral_unit = ""
    mult_raw = fields.get("mult", "")
    mult_norm = ""
    if mult_raw:
        mult_norm = _MULT_MAP.get(mult_raw, "")
        if not mult_norm:
            diags.append(Diagnostic("warning", "W-MULT",
                                    "%s: unknown multiplicity %r kept as-is"
                                    % (rid, mult_raw), lineno, rid))
    j_raw = fields.get("J", fields.get("j", ""))
    j_hz: Optional[float] = None
    if j_raw:
        j_val, j_unit = _split_value_unit(j_raw)
        if j_val is None:
            diags.append(Diagnostic("warning", "W-UNCONVERTIBLE",
                                    "%s: J=%r kept as-is" % (rid, j_raw),
                                    lineno, rid))
        elif j_unit and j_unit != "Hz":
            diags.append(Diagnostic("warning", "W-UNIT",
                                    "%s: J unit %r kept as-is"
                                    % (rid, j_unit), lineno, rid))
        else:
            j_hz = j_val
    status = "ok"
    if shift is None or fields.get("shift__unconvertible"):
        status = models.PEAK_UNCONVERTIBLE
    return {
        "id": rid,
        "nucleus": nucleus,
        "shift_ppm": shift,
        "shift_raw": fields.get("shift", ""),
        "shift_unit": shift_unit or "ppm",
        "intensity": intensity,
        "intensity_raw": intensity_raw or "",
        "integral_h": integral_num,
        "integral_raw": integral_raw or "",
        "integral_unit": integral_unit or ("H" if integral_raw else ""),
        "mult": mult_norm,
        "mult_raw": mult_raw,
        "j_hz": j_hz,
        "j_raw": j_raw,
        "note": fields.get("note", ""),
        "status": status,
    }


def _parse_edge(rid: str, fields: Dict[str, str], lineno: int, kind: str,
                diags: List[Diagnostic]) -> Dict[str, Any]:
    for key in ("from", "to"):
        if key not in fields:
            diags.append(Diagnostic("error", "E-MISSING-FIELD",
                                    "%s: missing %s" % (rid, key),
                                    lineno, rid))
    s1, u1 = _number_field(rid, fields, "shift1", lineno, diags,
                           required=False)
    s2, u2 = _number_field(rid, fields, "shift2", lineno, diags,
                           required=False)
    strength_raw = fields.get("strength", "")
    strength = None
    if strength_raw:
        try:
            strength = float(strength_raw)
        except ValueError:
            diags.append(Diagnostic("warning", "W-UNCONVERTIBLE",
                                    "%s: strength=%r kept as-is"
                                    % (rid, strength_raw), lineno, rid))
    return {
        "id": rid,
        "kind": kind,
        "from_peak": fields.get("from", ""),
        "to_peak": fields.get("to", ""),
        "shift1_ppm": s1,
        "shift1_raw": fields.get("shift1", ""),
        "shift1_unit": u1 or ("ppm" if fields.get("shift1") else ""),
        "shift2_ppm": s2,
        "shift2_raw": fields.get("shift2", ""),
        "shift2_unit": u2 or ("ppm" if fields.get("shift2") else ""),
        "strength": strength,
        "strength_raw": strength_raw,
        "note": fields.get("note", ""),
        "resolved": True,
    }


def _parse_directive(key: str, rest: str, lineno: int,
                     atoms: List[Dict[str, Any]], bonds: List[Dict[str, Any]],
                     equiv: List[Dict[str, Any]], shifts: Dict[str, float],
                     params: Dict[str, str], diags: List[Diagnostic],
                     current_nucleus: Optional[str]) -> None:
    if key == "atom":
        parts = rest.split()
        if not parts:
            diags.append(Diagnostic("error", "E-SYNTAX", "@atom needs an id",
                                    lineno))
            return
        kv = tokenize_kv(rest)
        atoms.append({
            "id": parts[0],
            "label": kv.get("label", parts[0]),
            "element": kv.get("element", "C"),
            "x": _maybe_float(kv.get("x"), parts[0], "x", lineno, diags),
            "y": _maybe_float(kv.get("y"), parts[0], "y", lineno, diags),
            "h_count": _maybe_int(kv.get("h", "0"), parts[0], lineno, diags),
            "ord": len(atoms),
        })
    elif key == "bond":
        parts = rest.split()
        if len(parts) < 2:
            diags.append(Diagnostic("error", "E-SYNTAX",
                                    "@bond needs two atom ids", lineno))
            return
        kv = tokenize_kv(rest)
        bonds.append({"a": parts[0], "b": parts[1],
                      "order": _maybe_int(kv.get("order", "1"),
                                          "bond", lineno, diags, fatal=False),
                      "ord": len(bonds)})
    elif key == "equiv":
        parts = rest.split()
        if len(parts) < 2:
            diags.append(Diagnostic("error", "E-SYNTAX",
                                    "@equiv needs group and members", lineno))
            return
        equiv.append({"id": parts[0], "members": parts[1:], "ord": len(equiv)})
    elif key == "param":
        kv = tokenize_kv(rest)
        params.update(kv)
    elif key == "shift":
        if "=" not in rest:
            diags.append(Diagnostic("error", "E-SYNTAX",
                                    "@shift needs NUC=value[ppm]", lineno))
            return
        name, raw = rest.split("=", 1)
        name = name.strip()
        val, unit = _split_value_unit(raw.strip())
        if val is None:
            diags.append(Diagnostic("error", "E-SHIFT",
                                    "cannot read shift %s=%r" % (name, raw),
                                    lineno))
            return
        if "@" in name:
            scoped = name
        elif name.startswith("C") or name.startswith("Q"):
            scoped = name
        else:
            scoped = "%s@%s" % (name, current_nucleus or "1H")
        shifts[scoped] = val


def _maybe_float(raw: Optional[str], ref: str, field: str, lineno: int,
                 diags: List[Diagnostic]) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        diags.append(Diagnostic("warning", "W-UNCONVERTIBLE",
                                "%s: %s=%r kept as-is" % (ref, field, raw),
                                lineno, ref))
        return None


def _maybe_int(raw: str, ref: str, lineno: int, diags: List[Diagnostic],
               fatal: bool = True) -> Optional[int]:
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        diags.append(Diagnostic("error" if fatal else "warning",
                                "W-UNCONVERTIBLE" if not fatal else "E-SYNTAX",
                                "%s: integer expected, got %r" % (ref, raw),
                                lineno, ref))
        return None


def _validate(doc_type: str, kind: str, nucleus: Optional[str],
              header: Dict[str, str], peaks: List[Dict[str, Any]],
              edges: List[Dict[str, Any]], atoms: List[Dict[str, Any]],
              bonds: List[Dict[str, Any]], equiv: List[Dict[str, Any]],
              diags: List[Diagnostic]) -> None:
    if not doc_type:
        diags.append(Diagnostic("error", "E-NO-HEADER",
                                "document must start with @spectrum, "
                                "@structure or @rulepack", 0))
        return
    if doc_type == "spectrum":
        if kind not in ("1d", "cosy", "hsqc", "hmbc"):
            diags.append(Diagnostic("error", "E-KIND",
                                    "@kind must be 1d/cosy/hsqc/hmbc", 0))
        if kind == "1d" and nucleus not in ("1H", "13C"):
            diags.append(Diagnostic("error", "E-NUCLEUS",
                                    "1d spectrum needs @nucleus 1H or 13C",
                                    0))
        for key in ("solvent", "temperature", "reference", "frequency-MHz"):
            if key not in header or not header[key]:
                diags.append(Diagnostic("error", "E-HEADER",
                                        "spectrum header missing @%s" % key,
                                        0))
        if kind == "1d" and not peaks:
            diags.append(Diagnostic("error", "E-NO-PEAKS",
                                    "1d spectrum has no peak records", 0))
        if kind in ("cosy", "hsqc", "hmbc") and not edges:
            diags.append(Diagnostic("error", "E-NO-EDGES",
                                    "%s spectrum has no edges" % kind, 0))
    if doc_type == "structure":
        ids = {a["id"] for a in atoms}
        if not atoms:
            diags.append(Diagnostic("error", "E-NO-ATOMS",
                                    "structure has no @atom records", 0))
        for bond in bonds:
            for endpoint in (bond["a"], bond["b"]):
                if endpoint not in ids:
                    diags.append(Diagnostic("error", "E-UNKNOWN-ATOM",
                                            "bond references unknown atom %s"
                                            % endpoint, bond["ord"], endpoint))
        members = {m for g in equiv for m in g["members"]}
        for member in members:
            if member not in ids:
                diags.append(Diagnostic("error", "E-UNKNOWN-ATOM",
                                        "@equiv references unknown atom %s"
                                        % member, 0, member))


def parse_sources(text: str, filename: str = "<input>"
                  ) -> Tuple[ParsedDocument, List[Dict[str, Any]]]:
    """Parse one document and attach stable ids to records.

    Returns (doc, records) where records are id-addressable raw values for
    the provenance layer (every displayed value gets a stable identity)."""
    doc = parse_document(text, filename)
    records: List[Dict[str, Any]] = []
    for coll_name, coll in (("peaks", doc.peaks), ("edges", doc.edges),
                            ("atoms", doc.atoms), ("bonds", doc.bonds),
                            ("equiv", doc.equiv_groups)):
        for item in coll:
            rid = item.get("id") or ("%s-%d" % (coll_name, item.get("ord", 0)))
            item["stable_id"] = stable_id("rec", filename, coll_name, rid)
            records.append({"stable_id": item["stable_id"],
                            "collection": coll_name, "ref": rid})
    return doc, records
