"""Parsers for the NMR workbench domain text formats.

All formats are copy-paste friendly plain text with a leading magic header.
The parser is deliberately strict about structure (so typos are reported with
line numbers) but lenient about *values*: a value that cannot be converted is
kept verbatim and flagged in ``diagnostics`` instead of being dropped.

Formats (see ``case_pack/`` for annotated examples):

* ``.npc``   - 1D peak list (shift, intensity, integration, multiplicity)
* ``.n2d``   - 2D correlation edge list (COSY/HSQC/HMBC)
* ``.nms``   - molecular skeleton (atoms, bonds, coordinates)
* ``.nsym``  - symmetry/equivalence groups
* ``.npcnd`` - experimental conditions (solvent, temperature, frequencies)
* ``.nprior``- expected shifts and multiplicities per nucleus
* ``.nexp``  - expected correlation edges (what the coupling network should show)
* ``.nmp``   - algorithm parameters (key = value)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import units

VALID_KINDS = {"peaks1d", "edges2d", "skeleton", "symmetry",
               "conditions", "prior", "expected", "params"}


class FormatError(ValueError):
    """Structural error: the file cannot be interpreted at all."""


@dataclass
class Diagnostic:
    line: int
    code: str
    message: str
    raw: str

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "code": self.code,
                "message": self.message, "raw": self.raw}


@dataclass
class ParseResult:
    kind: str
    version: str
    records: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "records": self.records,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "meta": self.meta,
        }


def _split_lines(text: str) -> list[tuple[int, str]]:
    return [(i, line.rstrip("\r\n"))
            for i, line in enumerate(text.splitlines(), start=1)]


def _read_header(lines: list[tuple[int, str]]) -> tuple[str, str, int]:
    idx = 0
    while idx < len(lines) and not lines[idx][1].strip():
        idx += 1
    if idx >= len(lines):
        raise FormatError("empty-file")
    lineno, header = lines[idx]
    parts = header.split()
    if len(parts) < 2 or parts[0] != "NMR-WORKBENCH":
        raise FormatError(f"bad-magic at line {lineno}: expected "
                          "'NMR-WORKBENCH <kind> vN'")
    kind = parts[1]
    if kind not in VALID_KINDS:
        raise FormatError(f"unknown-kind '{kind}' at line {lineno}")
    version = parts[2] if len(parts) > 2 else ""
    if not version.startswith("v"):
        raise FormatError(f"bad-version '{version}' at line {lineno}")
    return kind, version, idx + 1


def _records_after_header(lines, start, result: ParseResult) -> None:
    sections: dict[str, list[tuple[int, str]]] = {"_": []}
    current = "_"
    for lineno, raw in lines[start:]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            sections.setdefault(current, [])
            continue
        sections[current].append((lineno, raw))

    if result.kind == "params":
        _parse_params(sections["_"], result)
        return

    for section_name, rows in sections.items():
        if section_name == "_":
            for lineno, raw in rows:
                result.diagnostics.append(
                    Diagnostic(lineno, "stray-line",
                               "line outside a section", raw))
            continue
        header_row = None
        data_rows: list[tuple[int, str]] = []
        for lineno, raw in rows:
            cells = [c.strip() for c in raw.split("\t")]
            if cells and cells[0].startswith("@"):
                header_row = (lineno, cells)
            else:
                data_rows.append((lineno, cells))
        if header_row is None:
            for lineno, raw in rows:
                result.diagnostics.append(
                    Diagnostic(lineno, "missing-header",
                               f"section [{section_name}] has no @ header", raw))
            continue
        _, columns = header_row
        for lineno, cells in data_rows:
            if len(cells) != len(columns):
                result.diagnostics.append(
                    Diagnostic(lineno, "column-count",
                               f"expected {len(columns)} columns, got "
                               f"{len(cells)}", "\t".join(cells)))
                continue
            record = {col: val for col, val in zip(columns, cells)}
            record["_section"] = section_name
            record["_line"] = lineno
            result.records.append(record)


def _parse_params(rows, result: ParseResult) -> None:
    for lineno, raw in rows:
        if "=" not in raw:
            result.diagnostics.append(
                Diagnostic(lineno, "bad-param", "expected key = value", raw))
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        m = re.fullmatch(r"[a-z0-9_]+", key)
        if not m:
            result.diagnostics.append(
                Diagnostic(lineno, "bad-param-name", key, raw))
            continue
        result.meta[key] = value



# ---------------------------------------------------------------------------
# Domain builders: turn generic tabular records into typed domain objects.
# Nothing here is dropped; every unconvertible value becomes a Quantity with a
# diagnostic and every rejected row is reported on ParseResult.
# ---------------------------------------------------------------------------

def parse_document(text: str, *, declared_name: str = "") -> ParseResult:
    lines = _split_lines(text)
    kind, version, start = _read_header(lines)
    result = ParseResult(kind=kind, version=version)
    result.meta["source_name"] = declared_name
    _records_after_header(lines, start, result)
    _validate(result)
    return result


def _err(result: ParseResult, record: dict[str, Any], code: str, message: str) -> None:
    result.diagnostics.append(
        Diagnostic(record.get("_line", -1), code, message,
                   "\t".join(v for k, v in record.items() if not k.startswith("_"))))


def _require(result, record, column: str) -> Optional[str]:
    value = record.get(column, "")
    if value == "":
        _err(result, record, "missing-field", f"empty value for '{column}'")
        return None
    return value


def _validate(result: ParseResult) -> None:
    kind = result.kind
    for record in result.records:
        if kind == "peaks1d":
            _build_peak(result, record)
        elif kind == "edges2d":
            _build_edge(result, record)
        elif kind == "skeleton":
            _build_skeleton(result, record)
        elif kind == "symmetry":
            _build_symmetry(result, record)
        elif kind == "conditions":
            _build_condition(result, record)
        elif kind == "prior":
            _build_prior(result, record)
        elif kind == "expected":
            _build_expected(result, record)


def _basis_of(record) -> str:
    basis = record.get("reference", "TMS") or "TMS"
    return basis.strip()


def _build_peak(result, record) -> None:
    section = record["_section"].lower()
    nucleus = record.get("nucleus", "").strip()
    if nucleus not in {"1H", "13C"}:
        _err(result, record, "bad-nucleus", f"nucleus must be 1H or 13C, got '{nucleus}'")
    basis = _basis_of(record)
    shift = units.parse_shift(record.get("shift", ""), basis=basis)
    if shift.value is None:
        _err(result, record, "bad-shift", shift.diagnostic or "bad-shift")
    intensity = units.parse_intensity(record.get("intensity", ""))
    if intensity.value is None:
        _err(result, record, "bad-intensity", intensity.diagnostic or "bad-intensity")
    integration = units.parse_integration(record.get("integration", ""))
    if integration.value is None:
        _err(result, record, "bad-integration",
             integration.diagnostic or "bad-integration")
    multiplicity = record.get("multiplicity", "").strip()
    if not re.fullmatch(r"(s|d|t|q|p|h|hept|m|brs|dd|dt|td)+", multiplicity):
        _err(result, record, "bad-multiplicity",
             f"unrecognised multiplicity '{multiplicity}'")
    record["typed"] = {
        "nucleus": nucleus,
        "shift": shift.to_dict(),
        "intensity": intensity.to_dict(),
        "integration": integration.to_dict(),
        "multiplicity": multiplicity,
        "section": section,
        "annotation": record.get("annotation", ""),
        "line": record["_line"],
    }


def _build_edge(result, record) -> None:
    experiment = record.get("experiment", "").strip().upper()
    if experiment not in {"COSY", "HSQC", "HMBC"}:
        _err(result, record, "bad-experiment",
             f"experiment must be COSY/HSQC/HMBC, got '{experiment}'")
    basis = _basis_of(record)
    f1 = units.parse_shift(record.get("shift_f1", ""), basis=basis)
    f2 = units.parse_shift(record.get("shift_f2", ""), basis=basis)
    for axis, quantity in (("f1", f1), ("f2", f2)):
        if quantity.value is None:
            _err(result, record, f"bad-shift-{axis}",
                 quantity.diagnostic or "bad-shift")
    intensity = units.parse_intensity(record.get("intensity", ""))
    if intensity.value is None:
        _err(result, record, "bad-intensity",
             intensity.diagnostic or "bad-intensity")
    record["typed"] = {
        "experiment": experiment,
        "shift_f1": f1.to_dict(),
        "shift_f2": f2.to_dict(),
        "intensity": intensity.to_dict(),
        "annotation": record.get("annotation", ""),
        "line": record["_line"],
    }


def _coord(raw: str) -> tuple[Optional[float], Optional[str]]:
    try:
        return float(raw), None
    except ValueError:
        return None, f"coordinate-not-numeric:{raw}"


def _build_skeleton(result, record) -> None:
    section = record["_section"]
    if section == "atoms":
        atom_id = _require(result, record, "atom_id")
        element = _require(result, record, "element")
        if element and element not in {"H", "C", "O", "N", "S", "P", "F", "Cl", "Br"}:
            _err(result, record, "bad-element", f"element '{element}' not supported")
        x, dx = _coord(record.get("x", ""))
        y, dy = _coord(record.get("y", ""))
        for diag in (dx, dy):
            if diag:
                _err(result, record, "bad-coordinate", diag)
        record["typed"] = {
            "atom_id": atom_id,
            "element": element,
            "nucleus": record.get("nucleus", "").strip(),
            "x": x, "y": y,
            "label": record.get("label", ""),
            "line": record["_line"],
        }
    elif section == "bonds":
        a = _require(result, record, "atom_a")
        b = _require(result, record, "atom_b")
        order = record.get("order", "1").strip() or "1"
        if order not in {"1", "2", "3"}:
            _err(result, record, "bad-bond-order", f"order '{order}' not 1/2/3")
        record["typed"] = {"atom_a": a, "atom_b": b, "order": int(order),
                           "line": record["_line"]}
    else:
        _err(result, record, "unknown-section",
             f"skeleton section '[{section}]' must be atoms or bonds")


def _build_symmetry(result, record) -> None:
    group = _require(result, record, "group_id")
    atom_id = _require(result, record, "atom_id")
    kind = record.get("kind", "equivalent").strip() or "equivalent"
    if kind not in {"equivalent", "enantiotopic", "diastereotopic"}:
        _err(result, record, "bad-symmetry-kind",
             f"symmetry kind '{kind}' not recognised")
    record["typed"] = {"group_id": group, "atom_id": atom_id, "kind": kind,
                       "line": record["_line"]}


def _build_condition(result, record) -> None:
    key = _require(result, record, "key")
    raw_value = record.get("value", "")
    typed: dict[str, Any] = {"key": key, "raw": raw_value}
    if key in {"freq_h", "freq_c"}:
        q = units.parse_frequency(raw_value)
        if q.value is None:
            _err(result, record, "bad-frequency", q.diagnostic or "bad-frequency")
        typed["quantity"] = q.to_dict()
    elif key == "temperature":
        q = units.parse_temperature(raw_value)
        if q.value is None:
            _err(result, record, "bad-temperature", q.diagnostic or "bad-temperature")
        typed["quantity"] = q.to_dict()
    elif key in {"reference_h", "reference_c"}:
        try:
            typed["value"] = units.parse_reference(raw_value)
        except units.UnitError as exc:
            _err(result, record, "bad-reference", str(exc))
            typed["value"] = None
    elif key in {"solvent", "sample", "operator", "notes"}:
        typed["value"] = raw_value.strip()
    else:
        _err(result, record, "unknown-condition",
             f"condition key '{key}' not recognised")
        typed["value"] = raw_value
    record["typed"] = typed


def _build_prior(result, record) -> None:
    atom_id = _require(result, record, "atom_id")
    nucleus = record.get("nucleus", "").strip()
    if nucleus not in {"1H", "13C"}:
        _err(result, record, "bad-nucleus", f"nucleus must be 1H or 13C, got '{nucleus}'")
    basis = record.get("reference", "TMS").strip() or "TMS"
    shift = units.parse_shift(record.get("expected_shift", ""), basis=basis)
    if shift.value is None:
        _err(result, record, "bad-shift", shift.diagnostic or "bad-shift")
    tolerance_raw = record.get("tolerance", "")
    try:
        tolerance = float(tolerance_raw)
        if tolerance <= 0:
            raise ValueError
    except ValueError:
        _err(result, record, "bad-tolerance",
             f"tolerance '{tolerance_raw}' must be positive ppm")
        tolerance = None
    multiplicity = record.get("expected_multiplicity", "").strip()
    if multiplicity and not re.fullmatch(r"(s|d|t|q|p|h|hept|m|brs|dd|dt|td)+", multiplicity):
        _err(result, record, "bad-multiplicity",
             f"unrecognised multiplicity '{multiplicity}'")
    record["typed"] = {
        "atom_id": atom_id,
        "nucleus": nucleus,
        "expected_shift": shift.to_dict(),
        "tolerance": tolerance,
        "expected_multiplicity": multiplicity,
        "expected_integration": float(record["expected_integration"])
        if re.fullmatch(r"\d+(?:\.\d+)?", record.get("expected_integration", ""))
        else None,
        "line": record["_line"],
    }
    if record["typed"]["expected_integration"] is None:
        _err(result, record, "bad-integration",
             f"expected_integration '{record.get('expected_integration','')}' "
             "must be numeric")


def _build_expected(result, record) -> None:
    experiment = record.get("experiment", "").strip().upper()
    if experiment not in {"COSY", "HSQC", "HMBC"}:
        _err(result, record, "bad-experiment",
             f"experiment must be COSY/HSQC/HMBC, got '{experiment}'")
    a = _require(result, record, "atom_a")
    b = _require(result, record, "atom_b")
    record["typed"] = {
        "experiment": experiment, "atom_a": a, "atom_b": b,
        "note": record.get("note", ""), "line": record["_line"],
    }
