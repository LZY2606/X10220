"""Import layer: parses small copy-pastable TSV/JSON sources.

Raw text is stored verbatim and never overwritten; revisions arrive as new
versions linked through ``parent_id``. Values that cannot be converted stay in
place with their original representation plus a diagnostic - they are never
silently dropped to make plotting easier.
"""
from __future__ import annotations

import json

PEAK_KINDS = {"peaks-1h", "peaks-13c"}
EDGE_KINDS = {"edges-cosy", "edges-hsqc", "edges-hmbc"}
PARAM_KINDS = {"params-json"}
ALL_KINDS = PEAK_KINDS | EDGE_KINDS | PARAM_KINDS

FORMAT_VERSION = "nmrwb-src/1"


class FormatError(Exception):
    """Explicit, machine-readable format errors."""

    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        msg = "; ".join(d.get("message", "?") for d in diagnostics)
        super().__init__(msg)


def _diag(line, field, message, raw=None):
    d = {"line": line, "field": field, "message": message}
    if raw is not None:
        d["raw"] = raw
    return d


def _split_rows(text):
    headers = {}
    rows = []
    for n, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            body = line.lstrip()[1:].strip()
            if ":" in body:
                key, value = body.split(":", 1)
                headers[key.strip()] = value.strip()
            continue
        rows.append((n, line))
    return headers, rows


def _to_float(raw):
    try:
        return float(raw), None
    except (TypeError, ValueError):
        return None, "not_a_number"


def parse_source(kind: str, text: str) -> dict:
    """Parse one source document. Raises FormatError on structural problems;
    row-level conversion failures are returned as diagnostic rows instead."""
    if kind not in ALL_KINDS:
        raise FormatError([_diag(0, "kind", f"unknown source kind {kind!r}")])
    if not isinstance(text, str) or not text.strip():
        raise FormatError([_diag(0, "content", "empty source content")])
    if kind in PARAM_KINDS:
        return _parse_params(text)
    headers, rows = _split_rows(text)
    if not rows:
        raise FormatError([_diag(0, "header", "missing column header line")])
    header_line, header_text = rows[0]
    columns = [c.strip() for c in header_text.split("\t")]
    if kind in PEAK_KINDS:
        return _parse_peaks(kind, headers, columns, rows[1:], header_line)
    return _parse_edges(kind, headers, columns, rows[1:], header_line)


def _parse_peaks(kind, headers, columns, rows, header_line):
    required = ["label", "shift_ppm"]
    optional = ["intensity", "integral", "multiplicity"]
    unknown = [c for c in columns if c not in required + optional]
    if unknown:
        raise FormatError([_diag(header_line, "header", f"unknown column(s): {', '.join(unknown)}")])
    missing = [c for c in required if c not in columns]
    if missing:
        raise FormatError([_diag(header_line, "header", f"missing required column(s): {', '.join(missing)}")])
    idx = {c: columns.index(c) for c in columns}
    peaks, diagnostics = [], []
    seen_labels = set()
    for line_no, line in rows:
        cells = line.split("\t")
        if len(cells) != len(columns):
            raise FormatError([_diag(line_no, "row",
                                     f"expected {len(columns)} tab-separated cells, got {len(cells)}",
                                     raw=line)])
        label = cells[idx["label"]].strip()
        shift_raw = cells[idx["shift_ppm"]].strip()
        intensity_raw = cells[idx["intensity"]].strip() if "intensity" in idx else ""
        integral_raw = cells[idx["integral"]].strip() if "integral" in idx else ""
        mult = cells[idx["multiplicity"]].strip() if "multiplicity" in idx else ""
        status, diagnostic = "ok", None
        shift_ppm, err = _to_float(shift_raw)
        if not label:
            status, diagnostic = "diagnostic", "missing_label"
            label = f"line-{line_no}"
        elif label in seen_labels:
            raise FormatError([_diag(line_no, "label", f"duplicate peak label {label!r}")])
        elif err:
            # Unconvertible value: kept in place with its raw representation.
            status, diagnostic = "diagnostic", f"shift_ppm {err}"
        seen_labels.add(label)
        peak = {
            "label": label,
            "shift_raw": shift_raw,
            "shift_ppm": shift_ppm,
            "intensity_raw": intensity_raw,
            "integral_raw": integral_raw,
            "multiplicity": mult,
            "status": status,
            "diagnostic": diagnostic,
            "line_no": line_no,
        }
        peaks.append(peak)
        if diagnostic:
            diagnostics.append(_diag(line_no, label, diagnostic, raw=shift_raw))
    return {"kind": kind, "headers": headers, "peaks": peaks, "edges": [],
            "diagnostics": diagnostics}


def _parse_edges(kind, headers, columns, rows, header_line):
    required = ["a", "b"]
    optional = ["weight"]
    unknown = [c for c in columns if c not in required + optional]
    if unknown:
        raise FormatError([_diag(header_line, "header", f"unknown column(s): {', '.join(unknown)}")])
    missing = [c for c in required if c not in columns]
    if missing:
        raise FormatError([_diag(header_line, "header", f"missing required column(s): {', '.join(missing)}")])
    idx = {c: columns.index(c) for c in columns}
    edges, diagnostics = [], []
    for line_no, line in rows:
        cells = line.split("\t")
        if len(cells) != len(columns):
            raise FormatError([_diag(line_no, "row",
                                     f"expected {len(columns)} tab-separated cells, got {len(cells)}",
                                     raw=line)])
        a, b = cells[idx["a"]].strip(), cells[idx["b"]].strip()
        weight, err = (1.0, None)
        if "weight" in idx and cells[idx["weight"]].strip():
            weight, err = _to_float(cells[idx["weight"]].strip())
        if not a or not b:
            diagnostics.append(_diag(line_no, "edge", "missing endpoint", raw=line))
            continue
        if err:
            diagnostics.append(_diag(line_no, "weight", f"weight {err}; defaulted to 1.0"))
            weight = 1.0
        edges.append({"a": a, "b": b, "weight": weight, "line_no": line_no})
    return {"kind": kind, "headers": headers, "peaks": [], "edges": edges,
            "diagnostics": diagnostics}


def _parse_params(text):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormatError([_diag(exc.lineno, "json", f"invalid JSON: {exc.msg}")])
    if not isinstance(payload, dict):
        raise FormatError([_diag(0, "json", "params must be a JSON object")])
    problems = []
    for key in ("solvent", "reference"):
        if key not in payload:
            problems.append(_diag(0, key, f"missing required param {key!r}"))
    for key in ("frequency_mhz", "temperature_K"):
        if key in payload and not isinstance(payload[key], (int, float)):
            problems.append(_diag(0, key, f"param {key!r} must be numeric"))
    if problems:
        raise FormatError(problems)
    return {"kind": "params-json", "headers": {}, "peaks": [], "edges": [],
            "params": payload, "diagnostics": []}
