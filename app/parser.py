"""Import layer for the offline NMR case pack.

Accepts small, copy-pasteable text formats. Every submitted file is kept
verbatim (raw_content) and is immutable; revisions become new versions with
a parent link. Unconvertible values stay in their records with diagnostics
so nothing is dropped just to make plots render.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .idgen import hash_id, fingerprint
from .units import parse_quantity

SUPPORTED_KINDS = ("molecule", "nmr1d", "nmr2d")
CONDITION_KEYS = ("solvent", "temperature", "reference", "frequency",
                  "nucleus", "experiment", "f1_nucleus", "f2_nucleus")


@dataclass
class ParsedSource:
    kind: str
    filename: str
    format_version: str
    raw_content: str
    headers: List[Dict[str, str]]
    conditions: Dict[str, str]
    records: List[Dict[str, Any]]
    diagnostics: List[str] = field(default_factory=list)
    fp: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "filename": self.filename,
            "format_version": self.format_version,
            "raw_content": self.raw_content,
            "headers": self.headers,
            "conditions": dict(self.conditions),
            "records": self.records,
            "diagnostics": list(self.diagnostics),
            "fp": self.fp,
        }


def _record_id(prefix: str, filename: str, lineno: int, line: str) -> str:
    return f"{prefix}-L{lineno:03d}-{hash_id('', [filename, line])[2:8]}"


def depkeys_for(source: ParsedSource) -> List[str]:
    keys = [f"fp:{source.fp}"]
    for rec in source.records:
        keys.append(f"rec:{rec['id']}")
    for key, raw in sorted(source.conditions.items()):
        keys.append(f"cond:{key}={raw.strip()}")
    return keys


def parse_source(kind: str, filename: str, content: str) -> ParsedSource:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported source kind: {kind}")
    diagnostics: List[str] = []
    lines = content.splitlines()
    format_version = "unknown"
    if lines and lines[0].startswith("#"):
        format_version = lines[0].lstrip("#").strip()
    else:
        diagnostics.append("missing format header line, e.g. '# NMR-1D v1'")

    headers: List[Dict[str, str]] = []
    conditions: Dict[str, str] = {}
    records: List[Dict[str, Any]] = []

    for lineno, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        tag = tokens[0]

        if kind == "molecule" and tag == "atom":
            records.append(_parse_atom(filename, lineno, raw_line, tokens, diagnostics))
        elif kind == "molecule" and tag == "bond":
            if len(tokens) < 3:
                diagnostics.append(f"L{lineno}: bond needs two atom ids: {line!r}")
                continue
            rid = _record_id("bnd", filename, lineno, line)
            records.append({"type": "bond", "id": rid, "a": tokens[1],
                            "b": tokens[2], "raw_line": raw_line})
        elif kind == "molecule" and tag == "equiv":
            if len(tokens) < 3:
                diagnostics.append(f"L{lineno}: equiv needs a group id and members: {line!r}")
                continue
            rid = _record_id("eqv", filename, lineno, line)
            records.append({"type": "equiv", "id": rid, "group_id": tokens[1],
                            "members": tokens[2:], "raw_line": raw_line})
        elif kind == "nmr1d" and tag == "peak":
            records.append(_parse_peak(filename, lineno, raw_line, tokens, diagnostics))
        elif kind == "nmr2d" and tag == "edge":
            records.append(_parse_edge(filename, lineno, raw_line, tokens, diagnostics))
        elif tag in CONDITION_KEYS or tag in ("name", "molformula"):
            value = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
            headers.append({"key": tag, "value": value, "raw_line": raw_line, "lineno": lineno})
            if tag in CONDITION_KEYS:
                conditions[tag] = value
        else:
            diagnostics.append(f"L{lineno}: unrecognised directive {tag!r}; line retained as raw only")
            headers.append({"key": tag, "value": line[len(tag):].strip(),
                            "raw_line": raw_line, "lineno": lineno})

    _validate(kind, records, diagnostics)
    parsed = ParsedSource(kind=kind, filename=filename, format_version=format_version,
                          raw_content=content, headers=headers, conditions=conditions,
                          records=records, diagnostics=sorted(set(diagnostics)))
    parsed.fp = fingerprint(kind + "\n" + content)
    return parsed


def _parse_atom(filename, lineno, raw_line, tokens, diagnostics) -> dict:
    rid = _record_id("atm", filename, lineno, raw_line.strip())
    rec: Dict[str, Any] = {
        "type": "atom", "id": rid, "raw_line": raw_line,
        "atom_id": None, "element": None, "x": None, "y": None,
        "attach": None, "expected_shift": None, "multiplicity": None,
        "diagnostics": [],
    }
    if len(tokens) < 3:
        rec["diagnostics"].append("atom needs at least: atom <id> <element>")
        diagnostics.append(f"L{lineno}: malformed atom line")
        return rec
    rec["atom_id"] = tokens[1]
    rec["element"] = tokens[2]
    i = 3
    while i < len(tokens):
        key = tokens[i]
        if key in ("x", "y", "attach", "mult") and i + 1 >= len(tokens):
            rec["diagnostics"].append(f"atom key {key} missing value")
            i += 1
            continue
        if key in ("exp", "expected") and i + 1 < len(tokens):
            q = parse_quantity(tokens[i + 1], "shift", "ppm")
            rec["expected_shift"] = q.to_dict()
            rec["diagnostics"].extend(q.diagnostics)
            i += 2
        elif key == "attach":
            rec["attach"] = tokens[i + 1]
            i += 2
        elif key == "mult":
            rec["multiplicity"] = tokens[i + 1]
            i += 2
        elif key == "x":
            rec["x"] = _safe_float(tokens[i + 1], rec["diagnostics"], "x")
            i += 2
        elif key == "y":
            rec["y"] = _safe_float(tokens[i + 1], rec["diagnostics"], "y")
            i += 2
        else:
            rec["diagnostics"].append(f"unknown atom field {key!r}")
            i += 1
    if rec["diagnostics"]:
        diagnostics.append(f"L{lineno}: atom {rec['atom_id']} has unconvertible fields")
    return rec


def _safe_float(text, diag, label):
    try:
        return float(text)
    except ValueError:
        diag.append(f"non-numeric {label}: {text!r}")
        return None


def _parse_peak(filename, lineno, raw_line, tokens, diagnostics) -> dict:
    rid = _record_id("pk", filename, lineno, raw_line.strip())
    rec: Dict[str, Any] = {"type": "peak", "id": rid, "raw_line": raw_line,
                           "diagnostics": []}
    if len(tokens) < 6:
        rec["diagnostics"].append("peak expects: peak <shift ppm> <intensity> <int unit> <integral> <int unit> <mult>")
        diagnostics.append(f"L{lineno}: malformed peak line")
        return rec
    shift = parse_quantity(tokens[1], "shift", "ppm")
    intensity = parse_quantity(f"{tokens[2]} {tokens[3]}", "intensity", "a.u.")
    integral = parse_quantity(f"{tokens[4]} {tokens[5]}", "integral", "H")
    mult = tokens[6] if len(tokens) > 6 else "?"
    rec["shift"] = shift.to_dict()
    rec["intensity"] = intensity.to_dict()
    rec["integral"] = integral.to_dict()
    rec["multiplicity"] = mult
    for q in (shift, intensity, integral):
        rec["diagnostics"].extend(q.diagnostics)
    if rec["diagnostics"]:
        diagnostics.append(f"L{lineno}: peak has unconvertible fields (kept in place)")
    return rec


def _parse_edge(filename, lineno, raw_line, tokens, diagnostics) -> dict:
    rid = _record_id("edg", filename, lineno, raw_line.strip())
    rec: Dict[str, Any] = {"type": "edge", "id": rid, "raw_line": raw_line,
                           "diagnostics": []}
    if len(tokens) < 3:
        rec["diagnostics"].append("edge expects: edge <f1 ppm> <f2 ppm> [strength]")
        diagnostics.append(f"L{lineno}: malformed edge line")
        return rec
    f1 = parse_quantity(tokens[1], "shift", "ppm")
    f2 = parse_quantity(tokens[2], "shift", "ppm")
    rec["f1"] = f1.to_dict()
    rec["f2"] = f2.to_dict()
    rec["strength"] = tokens[3] if len(tokens) > 3 else "strong"
    rec["diagnostics"].extend(f1.diagnostics + f2.diagnostics)
    if rec["diagnostics"]:
        diagnostics.append(f"L{lineno}: edge has unconvertible coordinates")
    return rec


def _validate(kind: str, records: list, diagnostics: list) -> None:
    if kind == "molecule":
        atoms = [r for r in records if r["type"] == "atom"]
        ids = [r["atom_id"] for r in atoms]
        if len(ids) != len(set(ids)):
            diagnostics.append("duplicate atom ids in molecule")
        for rec in records:
            if rec["type"] == "atom" and rec["element"] in ("H",) and rec["attach"] is None:
                diagnostics.append(f"hydrogen {rec['atom_id']} has no attach target")
