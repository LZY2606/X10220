"""Import layer.

Rules honoured here:
- Raw text is stored verbatim and never overwritten; revisions are new
  versions linked by parent_id.
- Original representations (strings as they appeared in the file) are
  kept alongside parsed values; normalisation happens only in the
  derived layer.
- Values that cannot be converted stay in place with a diagnostic
  instead of being dropped.
- Clear format errors are reported, not swallowed.
"""

import json

PEAK_KINDS = {"peaks_1h": "1H", "peaks_13c": "13C"}
EDGE_KINDS = {"cosy": "COSY", "hsqc": "HSQC", "hmbc": "HMBC"}
ALL_KINDS = set(PEAK_KINDS) | set(EDGE_KINDS) | {"skeleton", "conditions"}


class FormatError(ValueError):
    """Raised for clear, user-facing format errors."""


def _float_or_none(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _data_lines(raw):
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield lineno, stripped


def parse(kind, fmt, raw):
    """Parse raw text for an input kind.

    Returns (tables, diagnostics, meta) where tables maps a table name to
    a list of row dicts ready for insertion.
    """
    if kind not in ALL_KINDS:
        raise FormatError("unknown input kind: %r (expected one of %s)"
                          % (kind, sorted(ALL_KINDS)))
    if kind in PEAK_KINDS:
        return _parse_peaks(kind, raw)
    if kind in EDGE_KINDS:
        return _parse_edges(kind, raw)
    if kind == "skeleton":
        return _parse_skeleton(raw)
    return _parse_conditions(raw)


def _parse_peaks(kind, raw):
    nucleus = PEAK_KINDS[kind]
    peaks, diagnostics = [], []
    order = 0
    for lineno, line in _data_lines(raw):
        fields = [f.strip() for f in line.split(",")]
        if fields[0].lower() == "label":
            continue  # header
        if len(fields) < 2:
            diagnostics.append({
                "line": lineno, "raw": line,
                "error": "expected at least 'label,shift' columns",
            })
            continue
        label, shift_raw = fields[0], fields[1]
        intensity_raw = fields[2] if len(fields) > 2 else ""
        integration_raw = fields[3] if len(fields) > 3 else ""
        multiplicity = fields[4] if len(fields) > 4 else ""
        if not label:
            diagnostics.append({"line": lineno, "raw": line,
                                "error": "empty peak label"})
            continue
        shift = _float_or_none(shift_raw)
        diagnostic = None
        if shift is None:
            diagnostic = "unconvertible_shift: %r kept as raw text" % shift_raw
            diagnostics.append({"line": lineno, "raw": line,
                                "error": diagnostic})
        integration = _float_or_none(integration_raw)
        if integration_raw and integration is None:
            diagnostics.append({"line": lineno, "raw": line,
                                "error": "unconvertible_integration: %r"
                                         % integration_raw})
        order += 1
        peaks.append({
            "ord": order, "label": label, "nucleus": nucleus,
            "shift_raw": shift_raw, "shift_ppm": shift,
            "intensity_raw": intensity_raw,
            "intensity": _float_or_none(intensity_raw),
            "integration_raw": integration_raw, "integration": integration,
            "multiplicity": multiplicity or None,
            "unit": "ppm",
            "basis": "chemical shift vs declared reference, as in source file",
            "diagnostic": diagnostic,
        })
    if not peaks and not diagnostics:
        raise FormatError("empty peak list")
    return {"peaks": peaks}, diagnostics, {"nucleus": nucleus}


def _parse_edges(kind, raw):
    edge_kind = EDGE_KINDS[kind]
    edges, diagnostics = [], []
    order = 0
    for lineno, line in _data_lines(raw):
        fields = [f.strip() for f in line.split(",")]
        if fields[0].lower() == "a":
            continue  # header
        if len(fields) < 2 or not fields[0] or not fields[1]:
            diagnostics.append({
                "line": lineno, "raw": line,
                "error": "expected 'a_label,b_label[,weight]'",
            })
            continue
        weight = _float_or_none(fields[2]) if len(fields) > 2 else 1.0
        if len(fields) > 2 and weight is None:
            diagnostics.append({"line": lineno, "raw": line,
                                "error": "unconvertible_weight: %r" % fields[2]})
        order += 1
        edges.append({
            "ord": order, "kind": edge_kind,
            "a_label": fields[0], "b_label": fields[1],
            "weight": weight if weight is not None else 1.0,
            "raw_json": json.dumps({"line": lineno, "raw": line},
                                   ensure_ascii=False),
        })
    if not edges and not diagnostics:
        raise FormatError("empty correlation list")
    return {"edges": edges}, diagnostics, {"edge_kind": edge_kind}


def _parse_skeleton(raw):
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FormatError("skeleton is not valid JSON: %s" % exc)
    if not isinstance(doc, dict) or not doc.get("atoms"):
        raise FormatError("skeleton JSON must contain an 'atoms' array")
    atoms, bonds = [], []
    basis = doc.get("basis", "chemical shift in ppm vs declared reference")
    for order, atom in enumerate(doc["atoms"], start=1):
        for field in ("key", "element", "shift_center", "shift_width"):
            if field not in atom:
                raise FormatError("skeleton atom %d missing %r" % (order, field))
        atoms.append({
            "ord": order, "atom_key": atom["key"], "element": atom["element"],
            "shift_center": float(atom["shift_center"]),
            "shift_width": float(atom["shift_width"]),
            "symmetry_group": atom.get("symmetry_group"),
            "bound_carbon": atom.get("bound_carbon"),
            "x": float(atom.get("x", 0.0)), "y": float(atom.get("y", 0.0)),
            "unit": "ppm", "basis": basis,
        })
    for order, pair in enumerate(doc.get("bonds", []), start=1):
        bonds.append({"ord": order, "a_key": pair[0], "b_key": pair[1]})
    meta = {
        "name": doc.get("name", "unnamed"),
        "basis": basis,
        "couplings": [list(map(str, p)) for p in doc.get("couplings", [])],
        "hmbc_links": [list(map(str, p)) for p in doc.get("hmbc_links", [])],
    }
    return {"atoms": atoms, "bonds": bonds}, [], meta


def _parse_conditions(raw):
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FormatError("conditions is not valid JSON: %s" % exc)
    if not isinstance(doc, dict):
        raise FormatError("conditions JSON must be an object")
    bias = _float_or_none(doc.get("shift_bias_ppm", 0.0))
    diagnostics = []
    if bias is None:
        diagnostics.append({"error": "unconvertible shift_bias_ppm, using 0.0"})
        bias = 0.0
    row = {
        "solvent": doc.get("solvent"),
        "frequency_mhz": _float_or_none(doc.get("frequency_mhz")),
        "temperature_c": _float_or_none(doc.get("temperature_c")),
        "reference": doc.get("reference"),
        "shift_bias_ppm": bias,
        "raw_json": json.dumps(doc, ensure_ascii=False, sort_keys=True),
    }
    return {"conditions": [row]}, diagnostics, {"conditions": row}
