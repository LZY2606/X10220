"""Importers for small, copy-pasteable NMR peak-list formats.

The raw layer keeps the original textual representation, header metadata and
units. Only the derived layer normalises numbers. Values that cannot be
converted stay in place and produce a diagnostic record; they are never dropped
to make plotting easier.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

MAGIC_PEAKLIST = "NMR-PEAKLIST/1.0"
MAGIC_CORR = "NMR-CORRELATIONS/1.0"

VALID_EXPERIMENTS = ("1H", "13C")
VALID_KINDS = ("COSY", "HSQC", "HMBC")

_PEAK_COLUMNS = ("shift", "intensity", "integration", "multiplicity")
_CORR_COLUMNS = ("shift_a", "shift_b", "intensity")

_MULTIPLICITY = {"s", "d", "t", "q", "m", "dd", "dt", "td", "brs", "br d", "sept", "h", "p"}


class ParseError(ValueError):
    """Explicit, user-facing format error with line number context."""

    def __init__(self, message: str, lineno: Optional[int] = None) -> None:
        if lineno is not None:
            message = "line %d: %s" % (lineno, message)
        super().__init__(message)
        self.lineno = lineno


def _strip_comment(line: str) -> Optional[str]:
    stripped = line.strip()
    if stripped.startswith("#"):
        return stripped[1:].strip()
    return None


def _split_metadata(text: str, magic: str) -> Tuple[Dict[str, str], List[str], List[int]]:
    metadata: Dict[str, str] = {}
    data: List[str] = []
    data_lines: List[int] = []
    saw_magic = False
    header_done = False
    for idx, raw in enumerate(text.splitlines(), start=1):
        comment = _strip_comment(raw)
        if comment is not None:
            if not saw_magic:
                if comment.strip() != magic:
                    raise ParseError("first non-empty line must be '# %s'" % magic, idx)
                saw_magic = True
                continue
            if header_done:
                continue
            if comment == "":
                header_done = True
                continue
            if ":" in comment:
                key, _, value = comment.partition(":")
                metadata[key.strip().lower()] = value.strip()
            continue
        if raw.strip() == "":
            continue
        if not saw_magic:
            raise ParseError("first non-empty line must be '# %s'" % magic, idx)
        header_done = True
        data.append(raw.strip())
        data_lines.append(idx)
    if not saw_magic:
        raise ParseError("missing magic header '# %s'" % magic)
    return metadata, data, data_lines


def _parse_float(token: str, lineno: int, column: str, unit: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    try:
        return float(token), None
    except ValueError:
        diagnostic = (
            "unconvertible %s value %r kept at original position; expected number"
            % (column, token)
        )
        if unit:
            diagnostic += " (unit %s)" % unit
        return None, "%s @ line %d" % (diagnostic, lineno)


def parse_peak_list(text: str) -> Dict[str, Any]:
    """Parse a 1D peak list.

    Returns a dict with ``metadata`` (including units/reference basis),
    ``columns`` and ``peaks``. Each peak keeps ``raw`` tokens, normalised
    numbers when possible and a ``diagnostics`` list.
    """
    metadata, data, data_lines = _split_metadata(text, MAGIC_PEAKLIST)
    if "experiment" not in metadata:
        raise ParseError("missing required metadata 'experiment' (1H or 13C)")
    experiment = metadata["experiment"].strip()
    if experiment not in VALID_EXPERIMENTS:
        raise ParseError("experiment must be one of %s" % ",".join(VALID_EXPERIMENTS))
    if not data:
        raise ParseError("no column header found after metadata block")

    header = [part.strip().lower() for part in data[0].split(",")]
    if header != list(_PEAK_COLUMNS):
        raise ParseError(
            "column header must be exactly: %s" % ",".join(_PEAK_COLUMNS), data_lines[0]
        )

    shift_unit = metadata.get("shift_unit", "ppm")
    peaks: List[Dict[str, Any]] = []
    diagnostics: List[str] = []
    for offset, rawline in enumerate(data[1:], start=1):
        lineno = data_lines[offset]
        tokens = [part.strip() for part in rawline.split(",")]
        if len(tokens) != 4:
            raise ParseError("expected 4 comma separated columns, got %d" % len(tokens), lineno)
        shift, shift_diag = _parse_float(tokens[0], lineno, "shift", shift_unit)
        intensity, int_diag = _parse_float(tokens[1], lineno, "intensity", None)
        integration, intg_diag = _parse_float(tokens[2], lineno, "integration", metadata.get("integration_unit"))
        multiplicity = tokens[3]
        row_diags = [d for d in (shift_diag, int_diag, intg_diag) if d]
        mult_ok = multiplicity in _MULTIPLICITY
        if not mult_ok:
            row_diags.append("unknown multiplicity code %r retained verbatim @ line %d" % (multiplicity, lineno))
        for diag in row_diags:
            diagnostics.append(diag)
        peaks.append(
            {
                "raw": tokens,
                "raw_line": rawline,
                "source_lineno": lineno,
                "shift": shift,
                "shift_unit": shift_unit,
                "intensity": intensity,
                "integration": integration,
                "integration_unit": metadata.get("integration_unit", "H-equivalents"),
                "multiplicity": multiplicity,
                "multiplicity_known": mult_ok,
                "diagnostics": row_diags,
            }
        )
    if not peaks:
        raise ParseError("peak list contains a header but no data rows")
    return {
        "kind": "peak_list",
        "metadata": metadata,
        "experiment": experiment,
        "shift_unit": shift_unit,
        "columns": list(_PEAK_COLUMNS),
        "peaks": peaks,
        "diagnostics": diagnostics,
    }


def parse_correlations(text: str) -> Dict[str, Any]:
    metadata, data, data_lines = _split_metadata(text, MAGIC_CORR)
    if "kind" not in metadata:
        raise ParseError("missing required metadata 'kind' (COSY/HSQC/HMBC)")
    kind = metadata["kind"].strip().upper()
    if kind not in VALID_KINDS:
        raise ParseError("kind must be one of %s" % ",".join(VALID_KINDS))
    if not data:
        raise ParseError("no column header found after metadata block")
    header = [part.strip().lower() for part in data[0].split(",")]
    if header != list(_CORR_COLUMNS):
        raise ParseError(
            "column header must be exactly: %s" % ",".join(_CORR_COLUMNS), data_lines[0]
        )
    shift_a_unit = metadata.get("shift_a_unit", "ppm")
    shift_b_unit = metadata.get("shift_b_unit", "ppm")
    edges: List[Dict[str, Any]] = []
    diagnostics: List[str] = []
    for offset, rawline in enumerate(data[1:], start=1):
        lineno = data_lines[offset]
        tokens = [part.strip() for part in rawline.split(",")]
        if len(tokens) != 3:
            raise ParseError("expected 3 comma separated columns, got %d" % len(tokens), lineno)
        a, da = _parse_float(tokens[0], lineno, "shift_a", shift_a_unit)
        b, db = _parse_float(tokens[1], lineno, "shift_b", shift_b_unit)
        intensity, di = _parse_float(tokens[2], lineno, "intensity", None)
        row_diags = [d for d in (da, db, di) if d]
        diagnostics.extend(row_diags)
        edges.append(
            {
                "raw": tokens,
                "raw_line": rawline,
                "source_lineno": lineno,
                "shift_a": a,
                "shift_a_unit": shift_a_unit,
                "shift_b": b,
                "shift_b_unit": shift_b_unit,
                "intensity": intensity,
                "diagnostics": row_diags,
            }
        )
    if not edges:
        raise ParseError("correlation file contains a header but no data rows")
    return {
        "kind_file": "correlations",
        "experiment_kind": kind,
        "metadata": metadata,
        "columns": list(_CORR_COLUMNS),
        "edges": edges,
        "diagnostics": diagnostics,
    }


def parse_skeleton(text: str) -> Dict[str, Any]:
    import json

    try:
        skeleton = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError("skeleton.json is not valid JSON: %s" % exc)
    if not isinstance(skeleton, dict):
        raise ParseError("skeleton root must be an object")
    for key in ("compound", "atoms", "bonds"):
        if key not in skeleton:
            raise ParseError("skeleton missing required key %r" % key)
    atoms = skeleton["atoms"]
    if not isinstance(atoms, list) or not atoms:
        raise ParseError("'atoms' must be a non-empty list")
    atom_ids = set()
    for atom in atoms:
        for key in ("id", "nucleus", "x", "y"):
            if key not in atom:
                raise ParseError("atom missing required key %r" % key)
        if atom["id"] in atom_ids:
            raise ParseError("duplicate atom id %r" % atom["id"])
        atom_ids.add(atom["id"])
        if atom["nucleus"] not in ("H", "C"):
            raise ParseError("atom %s nucleus must be H or C" % atom["id"])
        for coord in ("x", "y"):
            try:
                float(atom[coord])
            except (TypeError, ValueError):
                raise ParseError("atom %s coordinate %r must be a number" % (atom["id"], atom[coord]))
    bonds = skeleton["bonds"]
    if not isinstance(bonds, list):
        raise ParseError("'bonds' must be a list")
    for bond in bonds:
        for key in ("a", "b", "order"):
            if key not in bond:
                raise ParseError("bond missing required key %r" % key)
        if bond["a"] not in atom_ids or bond["b"] not in atom_ids:
            raise ParseError("bond references unknown atom: %r" % bond)
        if int(bond["order"]) not in (1, 2, 3):
            raise ParseError("bond order must be 1, 2 or 3")
    equivalence = skeleton.get("equivalence", [])
    if not isinstance(equivalence, list):
        raise ParseError("'equivalence' must be a list of groups")
    for group in equivalence:
        for member in group:
            if member not in atom_ids:
                raise ParseError("equivalence group references unknown atom %r" % member)
    predicted = skeleton.get("predicted_shifts", {})
    if not isinstance(predicted, dict):
        raise ParseError("'predicted_shifts' must map atom id -> ppm")
    for atom_id, shift in predicted.items():
        if atom_id not in atom_ids:
            raise ParseError("predicted shift for unknown atom %r" % atom_id)
        try:
            float(shift)
        except (TypeError, ValueError):
            raise ParseError("predicted shift for %s must be a number" % atom_id)
    skeleton.setdefault("coordinate_system", "svg 100x100 user units")
    return skeleton


def parse_params(text: str) -> Dict[str, Any]:
    import json

    try:
        params = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError("params JSON is invalid: %s" % exc)
    if not isinstance(params, dict):
        raise ParseError("params root must be an object")
    for key in ("algorithm_version", "weights", "tolerances"):
        if key not in params:
            raise ParseError("params missing required key %r" % key)
    weights, tolerances = params["weights"], params["tolerances"]
    for section in (weights, tolerances):
        if not isinstance(section, dict):
            raise ParseError("weights/tolerances must be objects")
    for key in ("shift_sigma_1h", "shift_sigma_13c"):
        if key not in tolerances:
            raise ParseError("tolerances missing %r" % key)
    offsets = params.get("reference_offsets", {"1H": 0.0, "13C": 0.0})
    for experiment in ("1H", "13C"):
        offsets.setdefault(experiment, 0.0)
        offsets[experiment] = float(offsets[experiment])
    params["reference_offsets"] = offsets
    params.setdefault("seed", 20260923)
    return params
