import csv
import io
import json

from .constants import SOURCE_KINDS, EDGE_KINDS
from .utils import short_hash, stable_id

PEAK_META_COLUMNS = ("solvent", "temperature", "reference", "instrument_frequency_mhz")
PEAK_META_HEADER = {
    "solvent": "solvent",
    "temperature": "temperature",
    "reference": "reference",
    "instrument_frequency_mhz": "instrument_frequency_mhz",
    "frequency_mhz": "instrument_frequency_mhz",
}


class FormatError(Exception):
    def __init__(self, message, row=None, column=None):
        super().__init__(message)
        self.message = message
        self.row = row
        self.column = column

    def to_dict(self):
        return {"message": self.message, "row": self.row, "column": self.column}


def _read_rows(text):
    text = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise FormatError("empty file: header row missing")
    headers = [h.strip() for h in reader.fieldnames]
    rows = []
    for raw in reader:
        rows.append({(k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
                     for k, v in raw.items()})
    return headers, rows


def _as_float(raw):
    if raw is None or raw == "":
        return None, "missing value"
    try:
        return float(raw), None
    except (TypeError, ValueError):
        return None, "cannot convert %r to number" % raw


def _parse_peaks(kind, headers, rows, source_uid):
    nucleus = "1H" if kind == "peaks_1h" else "13C"
    required = ["peak_id", "shift_ppm"]
    for col in required:
        if col not in headers:
            raise FormatError("required column %r missing" % col, column=col)

    meta = {}
    for header in headers:
        key = PEAK_META_HEADER.get(header)
        if key:
            vals = [r[header] for r in rows if r.get(header) not in (None, "")]
            if vals:
                meta[key] = {"raw": vals[0], "header": header}

    records = []
    diagnostics = []
    for index, row in enumerate(rows, start=2):
        pid = row.get("peak_id") or ""
        if not pid:
            raise FormatError("peak_id missing", row=index, column="peak_id")
        shift_raw = row.get("shift_ppm", "")
        shift, err = _as_float(shift_raw)
        rec = {
            "uid": stable_id("pk", (source_uid, pid)),
            "peak_id": pid,
            "nucleus": nucleus,
            "shift_ppm_raw": shift_raw,
            "shift_ppm": shift,
            "intensity_raw": row.get("intensity", ""),
            "integration_raw": row.get("integration", ""),
            "multiplicity_raw": row.get("multiplicity", ""),
            "row_number": index,
            "raw_row": dict(row),
            "convertible": err is None,
        }
        intensity, ierr = _as_float(row.get("intensity", ""))
        integration, gerr = _as_float(row.get("integration", ""))
        rec["intensity"] = intensity
        rec["integration"] = integration
        if err:
            diagnostics.append({"row": index, "column": "shift_ppm", "raw": shift_raw,
                                "diagnostic": err, "policy": "POL-010"})
        if ierr:
            diagnostics.append({"row": index, "column": "intensity", "raw": row.get("intensity", ""),
                                "diagnostic": ierr, "policy": "POL-010"})
        if gerr:
            diagnostics.append({"row": index, "column": "integration", "raw": row.get("integration", ""),
                                "diagnostic": gerr, "policy": "POL-010"})
        records.append(rec)
    return {"kind": kind, "nucleus": nucleus, "meta": meta, "records": records,
            "diagnostics": diagnostics}


def _parse_edges(kind, headers, rows, source_uid):
    coord = "h_ppm" if kind == "cosy" else "from_h_ppm"
    second = "h_ppm" if kind == "cosy" else "to_c_ppm"
    if kind == "cosy":
        required = ["edge_id", "h1_ppm", "h2_ppm"]
    else:
        required = ["edge_id", "from_h_ppm", "to_c_ppm"]
    for col in required:
        if col not in headers:
            raise FormatError("required column %r missing" % col, column=col)
    records = []
    diagnostics = []
    for index, row in enumerate(rows, start=2):
        eid = row.get("edge_id") or ""
        if not eid:
            raise FormatError("edge_id missing", row=index, column="edge_id")
        if kind == "cosy":
            a_raw, b_raw = row.get("h1_ppm", ""), row.get("h2_ppm", "")
            labels = ("h1_ppm", "h2_ppm")
            atom_from = row.get("from_atom", "")
            atom_to = row.get("to_atom", "")
        else:
            a_raw, b_raw = row.get("from_h_ppm", ""), row.get("to_c_ppm", "")
            labels = ("from_h_ppm", "to_c_ppm")
            atom_from = row.get("from_atom", "")
            atom_to = row.get("to_atom", "")
        a, ea = _as_float(a_raw)
        b, eb = _as_float(b_raw)
        rec = {
            "uid": stable_id("ed", (source_uid, eid)),
            "edge_id": eid,
            "kind": kind,
            "a_ppm_raw": a_raw,
            "b_ppm_raw": b_raw,
            "a_ppm": a,
            "b_ppm": b,
            "coord_labels": labels,
            "from_atom": atom_from,
            "to_atom": atom_to,
            "row_number": index,
            "raw_row": dict(row),
            "convertible": ea is None and eb is None,
        }
        if ea:
            diagnostics.append({"row": index, "column": labels[0], "raw": a_raw,
                                "diagnostic": ea, "policy": "POL-010"})
        if eb:
            diagnostics.append({"row": index, "column": labels[1], "raw": b_raw,
                                "diagnostic": eb, "policy": "POL-010"})
        records.append(rec)
    return {"kind": kind, "records": records, "diagnostics": diagnostics}


def _parse_molecule(text, source_uid):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormatError("invalid JSON molecule file: %s" % exc)
    for key in ("compound", "atoms"):
        if key not in data:
            raise FormatError("molecule file missing %r" % key)
    atoms_in = data["atoms"]
    if not isinstance(atoms_in, list) or not atoms_in:
        raise FormatError("molecule atoms must be a non-empty list")
    seen = set()
    atoms = []
    diagnostics = []
    for index, atom in enumerate(atoms_in):
        for key in ("atom_id", "nucleus"):
            if key not in atom:
                raise FormatError("atom #%s missing %r" % (index, key))
        aid = atom["atom_id"]
        if aid in seen:
            raise FormatError("duplicate atom_id %r" % aid)
        seen.add(aid)
        predicted = atom.get("predicted_shift_ppm")
        predicted_raw = atom.get("predicted_shift_ppm_raw",
                                "" if predicted is None else str(predicted))
        value, err = (None, None) if predicted is None else _as_float(predicted_raw)
        if err:
            diagnostics.append({"atom": aid, "column": "predicted_shift_ppm",
                                "raw": predicted_raw, "diagnostic": err, "policy": "POL-010"})
        coords = atom.get("coord")
        atoms.append({
            "uid": stable_id("at", (source_uid, aid)),
            "atom_id": aid,
            "nucleus": atom["nucleus"],
            "element": atom.get("element", ""),
            "predicted_shift_ppm_raw": predicted_raw,
            "predicted_shift_ppm": value,
            "equivalence_group": atom.get("equivalence_group"),
            "coord": coords if isinstance(coords, list) else None,
            "raw": dict(atom),
            "convertible": err is None,
        })
    groups = data.get("equivalence_groups", [])
    expected_edges = data.get("expected_edges", [])
    return {
        "kind": "molecule",
        "compound": data["compound"],
        "meta": data.get("meta", {}),
        "records": atoms,
        "equivalence_groups": groups,
        "expected_edges": expected_edges,
        "diagnostics": diagnostics,
    }


def parse_source(kind, filename, text):
    """Parse one raw file. Returns (source_dict, format_errors).

    Accepted rows are always preserved with their raw representation;
    unconvertible values stay in place and are flagged.
    """
    if kind not in SOURCE_KINDS:
        raise FormatError("unknown source kind %r" % (kind,))
    if not isinstance(text, str) or not text.strip():
        raise FormatError("file %s is empty" % filename)
    content_hash = short_hash(text)
    base = {
        "kind": kind,
        "filename": filename,
        "content_hash": content_hash,
        "raw_text": text,
    }
    if kind in ("peaks_1h", "peaks_13c"):
        headers, rows = _read_rows(text)
        parsed = _parse_peaks(kind, headers, rows, "PENDING")
    elif kind in EDGE_KINDS:
        headers, rows = _read_rows(text)
        parsed = _parse_edges(kind, headers, rows, "PENDING")
    else:
        parsed = _parse_molecule(text, "PENDING")
    base.update(parsed)
    return base


def recompute_record_ids(parsed, source_uid):
    """Re-stamp derived record stable ids once the source uid is known."""
    if parsed["kind"] in ("peaks_1h", "peaks_13c"):
        for rec in parsed["records"]:
            rec["uid"] = stable_id("pk", (source_uid, rec["peak_id"]))
    elif parsed["kind"] in EDGE_KINDS:
        for rec in parsed["records"]:
            rec["uid"] = stable_id("ed", (source_uid, rec["edge_id"]))
    else:
        for rec in parsed["records"]:
            rec["uid"] = stable_id("at", (source_uid, rec["atom_id"]))
    return parsed
