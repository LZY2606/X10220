"""Raw-source importer.

Input format is line-delimited JSON (``.nmr.jsonl``) so small samples are easy
to copy/paste and every format error can point at an exact line. The importer
is deliberately preservation-first:

* the original bytes/line text are retained verbatim,
* every value keeps its unit, coordinate reference and raw token,
* unconvertible values stay in place with a diagnostic (non-fatal),
* structural mistakes (bad JSON, missing fields, non-numeric numbers,
  duplicate ids, unknown record types) are fatal :class:`FormatErrors`.
"""

from __future__ import annotations

import hashlib
import json

from .errors import FormatErrors
from .quantities import Quantity, quantity_from_token, shift

SCHEMA_VERSION = "nmr-jsonl/1"
RECORD_TYPES = {
    "1h_peaks",
    "13c_peaks",
    "cosy",
    "hsqc",
    "hmbc",
    "molecule",
    "experiment_meta",
}
MULTIPLICITY_CODES = {
    "s": "s", "d": "d", "t": "t", "q": "q", "m": "m",
    "dd": "dd", "dt": "dt", "br s": "br s", "brs": "br s",
}


def _err(code: str, loc: str, message: str) -> dict:
    return {"code": code, "loc": loc, "message": message}


def parse_document(raw_text: str, source_file: str, content_version: str,
                   submitted_at_ms: int) -> dict:
    """Parse one source document into the preserved payload dict."""

    errors: list[dict] = []
    diagnostics: list[dict] = []
    if not raw_text.strip():
        raise FormatErrors([_err("empty_document", "$", "document has no content")])

    lines: list[tuple[int, str, dict]] = []
    record_type = None
    file_version = None
    parent_version = None
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        loc = f"line {line_no}"
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append(_err("invalid_json", loc, f"not valid JSON: {exc.msg}"))
            continue
        if not isinstance(row, dict):
            errors.append(_err("not_object", loc, "each line must be a JSON object"))
            continue
        rtype = row.get("record_type")
        if rtype not in RECORD_TYPES:
            errors.append(_err("unknown_record_type", loc,
                               f"record_type must be one of {sorted(RECORD_TYPES)}"))
            continue
        if record_type is None:
            record_type = rtype
            file_version = row.get("file_version")
            parent_version = row.get("parent_version")
            if not isinstance(file_version, str) or not file_version:
                errors.append(_err("missing_file_version", loc,
                                   "first record requires non-empty 'file_version'"))
            if "parent_version" in row and row["parent_version"] is not None \
                    and not isinstance(row["parent_version"], str):
                errors.append(_err("bad_parent_version", loc,
                                   "'parent_version' must be a string or null"))
        elif rtype != record_type:
            errors.append(_err("mixed_record_types", loc,
                               f"document started as '{record_type}', cannot mix '{rtype}'"))
        lines.append((line_no, raw_line, row))

    if record_type is None and not errors:
        errors.append(_err("empty_document", "$", "document has no records"))
    if content_version != file_version and file_version is not None:
        errors.append(_err("version_mismatch", "$",
                           f"submitted content_version '{content_version}' != file_version "
                           f"'{file_version}' declared inside file"))
    if errors:
        raise FormatErrors(errors)

    parse = {
        "1h_peaks": _parse_peak_doc,
        "13c_peaks": _parse_peak_doc,
        "cosy": _parse_corr_doc,
        "hsqc": _parse_corr_doc,
        "hmbc": _parse_corr_doc,
        "molecule": _parse_molecule_doc,
        "experiment_meta": _parse_meta_doc,
    }[record_type]

    records = parse(lines, errors, diagnostics, record_type)
    if errors:
        raise FormatErrors(errors)

    checksum = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": record_type,
        "source_file": source_file,
        "content_version": content_version,
        "parent_version": parent_version,
        "submitted_at_ms": submitted_at_ms,
        "encoding": "utf-8",
        "sha256": checksum,
        "raw_text": raw_text,
        "records": records,
        "diagnostics": diagnostics,
    }


def _number(row: dict, key: str, loc: str, errors: list[dict],
            allow_none: bool = False) -> float | None:
    if key not in row:
        if allow_none:
            return None
        errors.append(_err("missing_field", loc, f"missing '{key}'"))
        return None
    value = row[key]
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(_err("non_numeric", loc, f"'{key}' must be a number"))
        return None
    return float(value)


def _parse_peak_doc(lines, errors, diagnostics, record_type):
    nucleus = "1H" if record_type == "1h_peaks" else "13C"
    records = []
    seen_ids: set[str] = set()
    first = True
    for line_no, raw_line, row in lines:
        loc = f"line {line_no}"
        if first:
            first = False
            continue
        pid = row.get("id")
        if not isinstance(pid, str) or not pid:
            errors.append(_err("missing_id", loc, "peak requires string 'id'"))
            continue
        if pid in seen_ids:
            errors.append(_err("duplicate_id", loc, f"duplicate peak id '{pid}'"))
            continue
        seen_ids.add(pid)
        if "shift_ppm" not in row:
            errors.append(_err("missing_field", loc, "missing 'shift_ppm'"))
            shift_q = Quantity(None, "?", "")
        else:
            shift_q = shift(row["shift_ppm"], nucleus)
        if shift_q.diagnostic and shift_q.value is None:
            errors.append(_err("bad_shift", loc, shift_q.diagnostic))
        elif shift_q.diagnostic:
            diagnostics.append({"loc": loc, "id": pid,
                                "message": shift_q.diagnostic})
        intensity = _number(row, "intensity", loc, errors, allow_none=True)
        integration = _number(row, "integration", loc, errors, allow_none=True)
        mult_raw = row.get("multiplicity")
        multiplicity_raw = None
        multiplicity = None
        if mult_raw is not None:
            multiplicity_raw = str(mult_raw)
            multiplicity = MULTIPLICITY_CODES.get(multiplicity_raw.strip().lower())
            if multiplicity is None:
                multiplicity = "?"
                diagnostics.append({"loc": loc, "id": pid,
                                    "message": f"unknown multiplicity '{multiplicity_raw}' "
                                               "retained verbatim"})
        note = row.get("note")
        rec = {
            "line_no": line_no,
            "raw_line": raw_line,
            "id": pid,
            "shift": shift_q.to_dict(),
            "intensity_raw": intensity,
            "integration_raw": integration,
            "multiplicity_raw": multiplicity_raw,
            "multiplicity": multiplicity,
        }
        if isinstance(note, str):
            rec["note"] = note
        records.append(rec)
    return records


def _parse_corr_doc(lines, errors, diagnostics, record_type):
    records = []
    seen_pairs: set[tuple] = set()
    first = True
    for line_no, raw_line, row in lines:
        loc = f"line {line_no}"
        if first:
            first = False
            continue
        cid = row.get("id")
        if not isinstance(cid, str) or not cid:
            errors.append(_err("missing_id", loc, "correlation requires string 'id'"))
            continue
        if record_type == "cosy":
            peaks = row.get("peaks")
            if not isinstance(peaks, list) or len(peaks) != 2 \
                    or not all(isinstance(p, str) and p for p in peaks):
                errors.append(_err("bad_endpoints", loc,
                                   "COSY row needs 'peaks': [hId, hId]"))
                continue
            endpoints = {"a": peaks[0], "b": peaks[1]}
            key = tuple(sorted(peaks))
        else:
            href = row.get("h")
            cref = row.get("c")
            if not isinstance(href, str) or not isinstance(cref, str) \
                    or not href or not cref:
                kind = "HSQC" if record_type == "hsqc" else "HMBC"
                errors.append(_err("bad_endpoints", loc,
                                   f"{kind} row needs string 'h' and 'c'"))
                continue
            endpoints = {"h": href, "c": cref}
            key = (href, cref)
        if key in seen_pairs:
            diagnostics.append({"loc": loc, "id": cid,
                                "message": "duplicate correlation endpoint pair retained"})
        seen_pairs.add(key)
        strength = _number(row, "intensity", loc, errors, allow_none=True)
        rec = {
            "line_no": line_no,
            "raw_line": raw_line,
            "id": cid,
            "endpoints": endpoints,
            "intensity_raw": strength,
        }
        records.append(rec)
    return records


def _parse_molecule_doc(lines, errors, diagnostics, record_type):
    atoms, bonds = [], []
    seen_ids: set[str] = set()
    first = True
    for line_no, raw_line, row in lines:
        loc = f"line {line_no}"
        if first:
            first = False
            continue
        kind = row.get("kind")
        if kind == "atom":
            aid = row.get("id")
            element = row.get("element")
            if not isinstance(aid, str) or not aid:
                errors.append(_err("missing_id", loc, "atom requires string 'id'"))
                continue
            if aid in seen_ids:
                errors.append(_err("duplicate_id", loc, f"duplicate atom id '{aid}'"))
                continue
            seen_ids.add(aid)
            if element not in ("C", "H", "O", "N", "F", "P"):
                errors.append(_err("bad_element", loc, f"unsupported element '{element}'"))
                continue
            expected = _number(row, "expected_shift_ppm", loc, errors, allow_none=True)
            group = row.get("equivalent_group")
            if group is not None and not isinstance(group, str):
                errors.append(_err("bad_equivalent_group", loc,
                                   "'equivalent_group' must be a string"))
            atoms.append({
                "line_no": line_no,
                "raw_line": raw_line,
                "id": aid,
                "element": element,
                "expected_shift_ppm": expected,
                "equivalent_group": group,
                "label": row.get("label", aid),
            })
        elif kind == "bond":
            endpoints = row.get("atoms")
            order = row.get("order", 1)
            if not isinstance(endpoints, list) or len(endpoints) != 2 \
                    or not all(isinstance(a, str) for a in endpoints):
                errors.append(_err("bad_bond", loc, "bond needs 'atoms': [id, id]"))
                continue
            if not isinstance(order, int) or order not in (1, 2, 3):
                errors.append(_err("bad_bond_order", loc, "'order' must be 1, 2 or 3"))
                continue
            bonds.append({"line_no": line_no, "raw_line": raw_line,
                          "atoms": [endpoints[0], endpoints[1]], "order": order})
        else:
            errors.append(_err("bad_kind", loc,
                               "molecule rows need kind 'atom' or 'bond'"))
    if not atoms:
        errors.append(_err("no_atoms", "$", "molecule document contains no atoms"))
    return {"atoms": atoms, "bonds": bonds}


def _parse_meta_doc(lines, errors, diagnostics, record_type):
    if len(lines) != 1:
        # Extra meta rows are accepted but the first row is the effective one.
        diagnostics.append({"loc": f"line {lines[1][0]}",
                            "message": "additional meta row ignored; supersede via new version"})
    row = lines[0][2]
    freq = _number(row, "instrument_frequency_mhz", "line 1", errors)
    solvent = row.get("solvent")
    reference = row.get("reference_standard")
    temperature = _number(row, "temperature_k", "line 1", errors, allow_none=True)
    if not isinstance(solvent, str) or not solvent:
        errors.append(_err("missing_solvent", "line 1", "missing 'solvent'"))
    if not isinstance(reference, str) or not reference:
        errors.append(_err("missing_reference", "line 1",
                           "missing 'reference_standard'"))
    return [{
        "line_no": lines[0][0],
        "raw_line": lines[0][1],
        "solvent_raw": solvent,
        "temperature_k_raw": temperature,
        "reference_standard_raw": reference,
        "instrument_frequency_mhz": freq,
    }]
