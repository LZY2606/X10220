"""Import-layer parsers.

Raw text is always stored verbatim. Parsers only *read* it into a
structured view; values that cannot be converted stay in place as raw
strings and produce diagnostics instead of being silently dropped.
"""
from __future__ import annotations

from typing import Any

SUPPORTED = ("peaklist", "molskel", "params")


def _float(value: str):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kv(token: str) -> tuple[str, str] | None:
    if "=" in token:
        k, v = token.split("=", 1)
        return k.strip(), v.strip()
    return None


def parse_document(fmt: str, raw: str) -> dict[str, Any]:
    """Parse a raw document. Returns {meta, peaks, edges, missing, atoms,
    bonds, symmetry, values, diagnostics}. Raw text is never mutated."""
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    out: dict[str, Any] = {"meta": {}, "peaks": [], "edges": [], "missing": [],
                           "atoms": [], "bonds": [], "symmetry": [],
                           "values": {}, "diagnostics": []}
    if not lines or not lines[0].startswith("!nmrwb/"):
        out["diagnostics"].append({"message": "missing_or_unknown_header",
                                   "raw_value": lines[0] if lines else ""})
        return out
    header = lines[0][len("!nmrwb/"):]
    fmt_seen, _, ver = header.partition(" ")
    out["meta"]["format"] = fmt_seen
    out["meta"]["format_version"] = ver or "v1"
    if fmt_seen != fmt:
        out["diagnostics"].append({"message": f"format_mismatch:{fmt_seen}!={fmt}",
                                   "raw_value": lines[0]})
    body = lines[1:]
    if fmt == "peaklist":
        _parse_peaklist(body, out)
    elif fmt == "molskel":
        _parse_molskel(body, out)
    elif fmt == "params":
        _parse_params(body, out)
    else:
        out["diagnostics"].append({"message": f"unsupported_format:{fmt}", "raw_value": None})
    return out


def _parse_peaklist(lines, out):
    for ln in lines:
        head, *rest = ln.split()
        if head.endswith(":") and head != "peak":
            out["meta"][head[:-1]] = " ".join(rest)
        elif head == "peak":
            peak = {"peak_id": rest[0], "raw": ln, "shift_ppm": None,
                    "raw_shift": None, "intensity": None, "integration": None,
                    "multiplicity": None, "overlap": None}
            for tok in rest[1:]:
                kv = _kv(tok)
                if not kv:
                    continue
                key, val = kv
                if key == "shift_ppm":
                    peak["raw_shift"] = val
                    num = _float(val)
                    if num is None:
                        out["diagnostics"].append({
                            "message": f"unconvertible_shift:{peak['peak_id']}",
                            "raw_value": val})
                    peak["shift_ppm"] = num
                elif key == "intensity":
                    peak["intensity"] = _float(val)
                elif key == "integration":
                    peak["integration"] = _float(val)
                elif key == "multiplicity":
                    peak["multiplicity"] = val
                elif key == "overlap":
                    peak["overlap"] = val
            out["peaks"].append(peak)
        elif head == "edge" and len(rest) >= 3:
            out["edges"].append({"kind": rest[0], "a": rest[1], "b": rest[2], "raw": ln})
        elif head == "missing" and len(rest) >= 3:
            out["missing"].append({"kind": rest[0], "a": rest[1], "b": rest[2], "raw": ln})
            out["diagnostics"].append({
                "message": f"missing_edge:{rest[0]}:{rest[1]}:{rest[2]}",
                "raw_value": ln})
        else:
            out["diagnostics"].append({"message": "unparsed_line", "raw_value": ln})


def _parse_molskel(lines, out):
    for ln in lines:
        head, *rest = ln.split()
        if head == "atom":
            atom = {"atom_id": rest[0], "element": rest[1] if len(rest) > 1 else "?",
                    "pred_ppm": None, "attaches": None, "x": 0.0, "y": 0.0, "raw": ln}
            for tok in rest[2:]:
                kv = _kv(tok)
                if not kv:
                    continue
                key, val = kv
                if key == "pred_ppm":
                    num = _float(val)
                    if num is None:
                        out["diagnostics"].append({
                            "message": f"unconvertible_pred_shift:{atom['atom_id']}",
                            "raw_value": val})
                    atom["pred_ppm"] = num
                elif key == "attaches":
                    atom["attaches"] = val
                elif key == "x":
                    atom["x"] = _float(val) or 0.0
                elif key == "y":
                    atom["y"] = _float(val) or 0.0
            out["atoms"].append(atom)
        elif head == "bond" and len(rest) >= 2:
            out["bonds"].append({"a": rest[0], "b": rest[1]})
        elif head == "symmetry" and len(rest) >= 2:
            out["symmetry"].append({"group": rest[0], "members": rest[1:]})
        else:
            out["diagnostics"].append({"message": "unparsed_line", "raw_value": ln})


def _parse_params(lines, out):
    for ln in lines:
        head, _, val = ln.partition(":")
        head = head.strip()
        val = val.strip()
        if not head:
            continue
        num = _float(val)
        out["values"][head] = num if num is not None else val
