"""Peak-list import layer.

Raw text is parsed into a structured document. The original representation
(raw strings, original line) is always preserved; numeric normalisation
happens here but any value that cannot be converted stays in place with a
diagnostic instead of being dropped.
"""
from __future__ import annotations

PARSE_VERSION = "peaklist-parse/1.0"

KNOWN_SECTIONS = {"meta", "1H", "13C", "HSQC", "COSY", "HMBC"}


def _num(value, lineno, field, diagnostics):
    try:
        return float(value)
    except (TypeError, ValueError):
        diagnostics.append({
            "line": lineno,
            "level": "warning",
            "message": "cannot convert %s=%r to a number; value kept raw in place" % (field, value),
            "field": field,
            "value": value,
        })
        return None


def parse_peaklist(raw_text):
    """Parse a peak-list document. Returns a JSON-able dict.

    Sections:
      [meta]  key: value lines (solvent, frequency_mhz, temperature_k, reference)
      [1H]    id,shift_ppm,intensity,integration,multiplicity
      [13C]   id,shift_ppm,intensity
      [HSQC]  h,c        (correlation edges)
      [COSY]  h1,h2
      [HMBC]  h,c
    """
    diagnostics = []
    meta = {}
    protons, carbons = [], []
    hsqc, cosy, hmbc = [], [], []
    section = None
    header = None

    for lineno, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            header = None
            if section not in KNOWN_SECTIONS:
                diagnostics.append({
                    "line": lineno, "level": "error",
                    "message": "unknown section [%s]" % section, "raw": raw_line,
                })
                section = None
            continue
        if section is None:
            diagnostics.append({
                "line": lineno, "level": "error",
                "message": "content outside any section", "raw": raw_line,
            })
            continue
        if section == "meta":
            if ":" not in line:
                diagnostics.append({
                    "line": lineno, "level": "error",
                    "message": "meta line must be 'key: value'", "raw": raw_line,
                })
                continue
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()  # raw string preserved as-is
            continue

        cells = [c.strip() for c in line.split(",")]
        if header is None:
            header = cells
            continue
        row = dict(zip(header, cells))
        row["_raw"] = raw_line  # original representation

        if section == "1H":
            protons.append({
                "id": row.get("id", ""),
                "shift_ppm": _num(row.get("shift_ppm"), lineno, "shift_ppm", diagnostics),
                "shift_raw": row.get("shift_ppm"),
                "intensity": _num(row.get("intensity"), lineno, "intensity", diagnostics),
                "intensity_raw": row.get("intensity"),
                "integration": _num(row.get("integration"), lineno, "integration", diagnostics),
                "integration_raw": row.get("integration"),
                "multiplicity": row.get("multiplicity", ""),
                "raw": raw_line,
            })
        elif section == "13C":
            carbons.append({
                "id": row.get("id", ""),
                "shift_ppm": _num(row.get("shift_ppm"), lineno, "shift_ppm", diagnostics),
                "shift_raw": row.get("shift_ppm"),
                "intensity": _num(row.get("intensity"), lineno, "intensity", diagnostics),
                "intensity_raw": row.get("intensity"),
                "raw": raw_line,
            })
        elif section in ("HSQC", "COSY", "HMBC"):
            a = row.get(header[0], "")
            b = row.get(header[1], "") if len(header) > 1 else ""
            if not a or not b:
                diagnostics.append({
                    "line": lineno, "level": "error",
                    "message": "correlation edge needs two endpoints", "raw": raw_line,
                })
                continue
            edge = {"id": "%s:%s->%s" % (section, a, b), "a": a, "b": b, "raw": raw_line}
            {"HSQC": hsqc, "COSY": cosy, "HMBC": hmbc}[section].append(edge)

    errors = [d for d in diagnostics if d["level"] == "error"]
    return {
        "parse_version": PARSE_VERSION,
        "meta": meta,
        "protons": protons,
        "carbons": carbons,
        "hsqc": hsqc,
        "cosy": cosy,
        "hmbc": hmbc,
        "diagnostics": diagnostics,
        "ok": len(errors) == 0,
    }
