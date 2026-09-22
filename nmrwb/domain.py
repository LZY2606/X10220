from __future__ import annotations

import csv
import io
import json
import re
from typing import List, Optional, Tuple

from .core import fingerprint, stable_id

DOC_KINDS = {"molecule", "peaks", "edges", "conditions", "params"}
EDGE_KINDS = {"COSY", "HSQC", "HMBC"}
MULTIPLICITIES = {"s", "d", "t", "q", "m", "dd", "dt", "td", "ddd", "br", "sept"}


class IngestError(Exception):
    def __init__(self, messages: List[str]):
        super().__init__("; ".join(messages))
        self.messages = messages


def _diag(object_id: str, field: str, message: str, severity: str = "warning") -> dict:
    return {"diag_id": stable_id("diag", object_id, field, message),
            "object_id": object_id, "field": field,
            "message": message, "severity": severity}


_NUM = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(.*)$")


def parse_quantity(raw: str, object_id: str, field: str,
                   allowed_units: dict, default_unit: Optional[str],
                   diags: list) -> Tuple[Optional[float], Optional[str]]:
    """Parse '<number> <unit>'; unconvertible values stay raw with a diagnostic."""
    if raw is None:
        return None, None
    text = str(raw).strip()
    if text == "":
        return None, None
    m = _NUM.match(text)
    if not m:
        diags.append(_diag(object_id, field,
                           "value %r is not numeric; kept as raw, excluded from normalization" % text))
        return None, None
    value = float(m.group(1))
    unit = m.group(2).strip() or default_unit
    if unit is None:
        return value, None
    if unit not in allowed_units:
        diags.append(_diag(object_id, field,
                           "unit %r not convertible to %s; kept as raw" % (unit, default_unit)))
        return None, None
    return allowed_units[unit](value), default_unit


def _ppm(v): return float(v)
def _identity(v): return float(v)
def _to_kelvin(v): return float(v) + 273.15
def _kelvin(v): return float(v)

SHIFT_UNITS = {"ppm": _ppm}
TEMP_UNITS = {"°C": _to_kelvin, "C": _to_kelvin, "K": _kelvin}
FREQ_UNITS = {"MHz": _identity}
PLAIN_UNITS = {"": _identity, "arb": _identity, "nuclei": _identity}


# ---------------------------------------------------------------- molecule
def parse_molecule(raw: str, doc_id: str):
    diags: list = []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise IngestError(["molecule: invalid JSON: %s" % e])
    sites, bonds = data.get("sites"), data.get("bonds", [])
    if not isinstance(sites, list) or not sites:
        raise IngestError(["molecule: 'sites' must be a non-empty list"])
    out_sites = []
    seen = set()
    for s in sites:
        for key in ("site", "nucleus", "pred_ppm"):
            if key not in s:
                raise IngestError(["molecule: site missing key %r" % key])
        if s["site"] in seen:
            raise IngestError(["molecule: duplicate site %r" % s["site"]])
        seen.add(s["site"])
        out_sites.append({
            "site": str(s["site"]),
            "nucleus": str(s["nucleus"]),
            "pred_ppm": float(s["pred_ppm"]),
            "symmetry_group": s.get("symmetry_group"),
            "x": float(s.get("x", 0.0)),
            "y": float(s.get("y", 0.0)),
        })
    site_names = {s["site"] for s in out_sites}
    out_bonds = []
    for b in bonds:
        if not isinstance(b, list) or len(b) != 2 or b[0] not in site_names or b[1] not in site_names:
            raise IngestError(["molecule: invalid bond %r" % (b,)])
        out_bonds.append([str(b[0]), str(b[1])])
    return {"sites": out_sites, "bonds": out_bonds}, diags


# ---------------------------------------------------------------- peaks
PEAK_COLUMNS = ["peak_id", "nucleus", "shift", "intensity", "integral", "multiplicity", "overlap_group"]


def parse_peaks(raw: str, doc_id: str):
    diags: list = []
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        raise IngestError(["peaks: empty CSV"])
    missing = [c for c in ("peak_id", "nucleus", "shift") if c not in reader.fieldnames]
    if missing:
        raise IngestError(["peaks: missing required columns %s" % missing])
    peaks, seen = [], set()
    for i, row in enumerate(reader, start=2):
        pid = (row.get("peak_id") or "").strip()
        if not pid:
            raise IngestError(["peaks: line %d: empty peak_id" % i])
        if pid in seen:
            raise IngestError(["peaks: duplicate peak_id %r" % pid])
        seen.add(pid)
        nucleus = (row.get("nucleus") or "").strip()
        if nucleus not in ("1H", "13C"):
            raise IngestError(["peaks: line %d: unsupported nucleus %r" % (i, nucleus)])
        shift_raw = (row.get("shift") or "").strip()
        shift_ppm, _ = parse_quantity(shift_raw, pid, "shift", SHIFT_UNITS, "ppm", diags)
        inten_raw = (row.get("intensity") or "").strip()
        intensity, _ = parse_quantity(inten_raw, pid, "intensity", PLAIN_UNITS, "arb", diags)
        integ_raw = (row.get("integral") or "").strip()
        integral, _ = parse_quantity(integ_raw, pid, "integral", PLAIN_UNITS, "nuclei", diags)
        mult = (row.get("multiplicity") or "").strip()
        if mult and mult not in MULTIPLICITIES:
            diags.append(_diag(pid, "multiplicity",
                               "unrecognized multiplicity %r; kept as raw" % mult))
        peaks.append({
            "peak_id": pid, "nucleus": nucleus,
            "shift_raw": shift_raw, "shift_ppm": shift_ppm,
            "intensity_raw": inten_raw, "intensity": intensity,
            "integral_raw": integ_raw, "integral": integral,
            "multiplicity": mult,
            "overlap_group": (row.get("overlap_group") or "").strip() or None,
        })
    if not peaks:
        raise IngestError(["peaks: no data rows"])
    return {"peaks": peaks}, diags


# ---------------------------------------------------------------- edges
def parse_edges(raw: str, doc_id: str):
    diags: list = []
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        raise IngestError(["edges: empty CSV"])
    missing = [c for c in ("edge_id", "kind", "peak_a", "peak_b") if c not in reader.fieldnames]
    if missing:
        raise IngestError(["edges: missing required columns %s" % missing])
    edges, seen = [], set()
    for i, row in enumerate(reader, start=2):
        eid = (row.get("edge_id") or "").strip()
        kind = (row.get("kind") or "").strip().upper()
        if kind not in EDGE_KINDS:
            raise IngestError(["edges: line %d: unknown kind %r (expected COSY/HSQC/HMBC)" % (i, kind)])
        if not eid or eid in seen:
            raise IngestError(["edges: line %d: missing or duplicate edge_id" % i])
        seen.add(eid)
        edges.append({"edge_id": eid, "kind": kind,
                      "peak_a": (row.get("peak_a") or "").strip(),
                      "peak_b": (row.get("peak_b") or "").strip()})
    return {"edges": edges}, diags


# ---------------------------------------------------------------- conditions
def parse_conditions(raw: str, doc_id: str):
    diags: list = []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise IngestError(["conditions: invalid JSON: %s" % e])
    if not isinstance(data, dict) or not data:
        raise IngestError(["conditions: expected a JSON object of condition entries"])
    out = {}
    for key, entry in data.items():
        if not isinstance(entry, dict) or "value" not in entry:
            raise IngestError(["conditions: entry %r must be an object with 'value'" % key])
        affects = entry.get("affects", ["1H", "13C"])
        value_raw = str(entry["value"])
        normalized, unit, basis = None, None, None
        if key.startswith("temperature"):
            normalized, unit = parse_quantity(value_raw, doc_id, key, TEMP_UNITS, "K", diags)
            basis = "K"
        elif key.startswith("frequency"):
            normalized, unit = parse_quantity(value_raw, doc_id, key, FREQ_UNITS, "MHz", diags)
            basis = "MHz"
        out[key] = {"value_raw": value_raw, "normalized": normalized,
                    "unit": unit or basis, "affects": list(affects)}
    return {"conditions": out}, diags


# ---------------------------------------------------------------- params
def parse_params(raw: str, doc_id: str):
    diags: list = []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise IngestError(["params: invalid JSON: %s" % e])
    if "tolerance_ppm" not in data:
        raise IngestError(["params: missing 'tolerance_ppm'"])
    tol = float(data["tolerance_ppm"])
    if tol <= 0:
        raise IngestError(["params: tolerance_ppm must be > 0"])
    return {"tolerance_ppm": tol}, diags


PARSERS = {
    "molecule": parse_molecule,
    "peaks": parse_peaks,
    "edges": parse_edges,
    "conditions": parse_conditions,
    "params": parse_params,
}


def parse_document(kind: str, raw: str, doc_id: str):
    if kind not in PARSERS:
        raise IngestError(["unknown document kind %r" % kind])
    return PARSERS[kind](raw, doc_id)


def params_fingerprint(parsed: dict, algo_version: str) -> str:
    return fingerprint({"params": parsed, "algo": algo_version})
