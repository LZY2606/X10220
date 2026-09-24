"""Import layer.

The raw file text is always preserved verbatim. Parsing produces a
``ParsedFile`` that keeps both ``raw_rows`` (original strings) and a normalized
layer. A value that cannot be converted is *kept in place* with a diagnostic;
it is never dropped to make plotting easier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ids import short_hash

PARSER_VERSION = "parser-1.0.0"

FILE_KINDS = ("molecule", "peaks_1h", "peaks_13c", "cosy", "hsqc", "hmbc")

MULTIPLICITIES = {"s", "d", "t", "q", "m", "dd", "dt", "br", "br s", "hept", "sept"}


class FormatError(ValueError):
    """Raised when an input file cannot be parsed at all."""


@dataclass
class Diagnostic:
    level: str  # "error" | "warning"
    code: str
    message: str
    location: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "location": self.location,
        }


@dataclass
class ParsedFile:
    kind: str
    filename: str
    parser_version: str
    header: dict[str, str]
    raw_rows: list[dict[str, str]]
    records: list[dict[str, Any]]
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(d.level == "error" for d in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "filename": self.filename,
            "parser_version": self.parser_version,
            "header": self.header,
            "raw_rows": self.raw_rows,
            "records": self.records,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


def _split_lines(raw: str) -> tuple[list[str], list[str]]:
    header_lines: list[str] = []
    body_lines: list[str] = []
    for line in raw.splitlines():
        if line.strip() == "":
            continue
        if line.lstrip().startswith("#"):
            header_lines.append(line.lstrip()[1:].strip())
        else:
            body_lines.append(line)
    return header_lines, body_lines


def _parse_header(header_lines: list[str]) -> dict[str, str]:
    header: dict[str, str] = {}
    for line in header_lines:
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        header[key.strip()] = val.strip()
    return header


def _parse_csv(body_lines: list[str]) -> list[dict[str, str]]:
    if not body_lines:
        raise FormatError("file has no table rows")
    columns = [c.strip() for c in body_lines[0].split(",")]
    if len(columns) != len(set(columns)):
        raise FormatError(f"duplicate columns in header: {columns}")
    rows: list[dict[str, str]] = []
    for n, line in enumerate(body_lines[1:], start=2):
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != len(columns):
            raise FormatError(
                f"line {n}: expected {len(columns)} columns, found {len(cells)}"
            )
        rows.append(dict(zip(columns, cells)))
    return rows


def _float_or_none(
    value: str,
    diags: list[Diagnostic],
    location: str,
    field_name: str,
) -> float | None:
    try:
        return float(value)
    except ValueError:
        diags.append(
            Diagnostic(
                "error",
                "non_numeric_value",
                f"{field_name}={value!r} is not a number; value kept at {location}",
                location,
            )
        )
        return None


def _require_columns(rows: list[dict[str, str]], required: set[str]) -> None:
    if not rows:
        raise FormatError("no data rows")
    missing = required - set(rows[0])
    if missing:
        raise FormatError(f"missing required columns: {sorted(missing)}")


def _parse_molecule(raw: str, filename: str) -> ParsedFile:
    import json

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FormatError(f"molecule JSON invalid: {exc}") from exc
    for key in ("molecule_id", "sites", "bonds"):
        if key not in doc:
            raise FormatError(f"molecule file missing {key!r}")
    diags: list[Diagnostic] = []
    site_ids = set()
    records: list[dict[str, Any]] = []
    for site in doc["sites"]:
        for key in ("id", "element", "kind", "x", "y"):
            if key not in site:
                raise FormatError(f"site missing {key!r}: {site}")
        if site["id"] in site_ids:
            raise FormatError(f"duplicate site id {site['id']}")
        site_ids.add(site["id"])
        rec = dict(site)
        rec["symmetry_group"] = site.get("symmetry_group")
        rec["shift"] = dict(site.get("shift", {}))
        records.append(rec)
    for bond in doc["bonds"]:
        if bond.get("a") not in site_ids or bond.get("b") not in site_ids:
            raise FormatError(f"bond references unknown site: {bond}")
    header = {k: str(v) for k, v in doc.items() if not isinstance(v, (list, dict))}
    return ParsedFile(
        "molecule",
        filename,
        PARSER_VERSION,
        header,
        [{"raw": raw}],
        [{"doc": doc, "sites": records}],
        diags,
    )


def _norm_1h(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    _require_columns(rows, {"peak_id", "shift_ppm"})
    records: list[dict[str, Any]] = []
    diags: list[Diagnostic] = []
    for i, row in enumerate(rows):
        loc = f"row {i + 1}({row.get('peak_id', '?')})"
        shift = _float_or_none(row["shift_ppm"], diags, loc, "shift_ppm")
        intensity = None
        if "intensity" in row and row["intensity"] != "":
            intensity = _float_or_none(row["intensity"], diags, loc, "intensity")
        integral = None
        if "integral_h" in row and row["integral_h"] != "":
            integral = _float_or_none(row["integral_h"], diags, loc, "integral_h")
        mult = row.get("multiplicity", "").strip()
        if mult and mult not in MULTIPLICITIES:
            diags.append(
                Diagnostic(
                    "warning",
                    "unknown_multiplicity",
                    f"multiplicity={mult!r} not in standard set; raw value kept",
                    loc,
                )
            )
        records.append(
            {
                "peak_id": row["peak_id"],
                "shift_ppm_raw": row["shift_ppm"],
                "shift_ppm": shift,
                "unit": "ppm",
                "intensity_raw": row.get("intensity", ""),
                "intensity": intensity,
                "integral_h_raw": row.get("integral_h", ""),
                "integral_h": integral,
                "multiplicity_raw": mult,
                "multiplicity": mult,
                "label": row.get("label", ""),
                "raw": row,
            }
        )
    return records, diags


def _norm_13c(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    _require_columns(rows, {"peak_id", "shift_ppm"})
    records: list[dict[str, Any]] = []
    diags: list[Diagnostic] = []
    for i, row in enumerate(rows):
        loc = f"row {i + 1}({row.get('peak_id', '?')})"
        shift = _float_or_none(row["shift_ppm"], diags, loc, "shift_ppm")
        intensity = None
        if "intensity" in row and row["intensity"] != "":
            intensity = _float_or_none(row["intensity"], diags, loc, "intensity")
        records.append(
            {
                "peak_id": row["peak_id"],
                "shift_ppm_raw": row["shift_ppm"],
                "shift_ppm": shift,
                "unit": "ppm",
                "intensity_raw": row.get("intensity", ""),
                "intensity": intensity,
                "kind_raw": row.get("kind", ""),
                "kind": row.get("kind", ""),
                "label": row.get("label", ""),
                "raw": row,
            }
        )
    return records, diags


def _norm_corr(
    rows: list[dict[str, str]],
    kind: str,
) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    if kind == "cosy":
        required = {"edge_id", "shift1_ppm", "shift2_ppm"}
        coords = ("shift1_ppm", "shift2_ppm")
    else:
        required = {"edge_id", "h_ppm", "c_ppm"}
        coords = ("h_ppm", "c_ppm")
    _require_columns(rows, required)
    records: list[dict[str, Any]] = []
    diags: list[Diagnostic] = []
    for i, row in enumerate(rows):
        loc = f"row {i + 1}({row.get('edge_id', '?')})"
        norm: dict[str, Any] = {"edge_id": row["edge_id"], "raw": row}
        strength = None
        if "strength" in row and row["strength"] != "":
            strength = _float_or_none(row["strength"], diags, loc, "strength")
        norm["strength"] = strength
        norm["strength_raw"] = row.get("strength", "")
        for coord in coords:
            norm[coord + "_raw"] = row[coord]
            norm[coord] = _float_or_none(row[coord], diags, loc, coord)
            norm[coord.replace("_ppm", "") + "_unit"] = "ppm"
        records.append(norm)
    return records, diags


_NORMALIZERS = {
    "peaks_1h": _norm_1h,
    "peaks_13c": _norm_13c,
    "cosy": lambda rows: _norm_corr(rows, "cosy"),
    "hsqc": lambda rows: _norm_corr(rows, "hsqc"),
    "hmbc": lambda rows: _norm_corr(rows, "hmbc"),
}


def parse(raw: str, kind: str, filename: str) -> ParsedFile:
    if kind not in FILE_KINDS:
        raise FormatError(f"unknown file kind {kind!r}")
    if kind == "molecule":
        return _parse_molecule(raw, filename)
    header_lines, body_lines = _split_lines(raw)
    header = _parse_header(header_lines)
    for required_header in ("nucleus", "spectrometer_frequency_mhz", "solvent"):
        if required_header not in header:
            raise FormatError(f"header missing #{required_header}: value")
    try:
        float(header["spectrometer_frequency_mhz"])
    except ValueError:
        raise FormatError("spectrometer_frequency_mhz must be numeric") from None
    raw_table = _parse_csv(body_lines)
    records, diags = _NORMALIZERS[kind](raw_table)
    return ParsedFile(
        kind,
        filename,
        PARSER_VERSION,
        header,
        raw_table,
        records,
        diags,
    )


def parsed_fingerprint(parsed: ParsedFile) -> str:
    body = {
        "kind": parsed.kind,
        "header": parsed.header,
        "records": parsed.to_dict()["records"],
    }
    return "fp_" + short_hash(body)
