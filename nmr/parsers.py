from __future__ import annotations

import csv
import io
import json
import re

from .models import Measurement, ParseIssue, ParsedSource


class SourceFormatError(ValueError):
    def __init__(self, issues: list[ParseIssue]):
        super().__init__("; ".join(issue.message for issue in issues))
        self.issues = issues


SHIFT_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(ppm)?\s*$", re.I)
FREQUENCY_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(MHz|Hz)\s*$", re.I)
TEMPERATURE_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(K|C)\s*$", re.I)


def parse_source(filename: str, content: str) -> ParsedSource:
    if filename.endswith(".json"):
        return parse_molecule_json(filename, content)
    return parse_peak_list(filename, content)


def parse_peak_list(filename: str, content: str) -> ParsedSource:
    issues: list[ParseIssue] = []
    lines = content.splitlines()
    if not lines:
        raise SourceFormatError([ParseIssue("error", "EMPTY_FILE", None, None, "source file is empty")])

    declared = _read_header(lines[0])
    headers: dict[str, str] = {}
    column_line: int | None = None
    for index, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        if line.startswith("#"):
            key, value = _read_metadata_line(line, index, issues)
            if key:
                headers[key] = value
            continue
        column_line = index
        break

    if column_line is None:
        issues.append(ParseIssue("error", "MISSING_TABLE", None, None, "no CSV table was found"))
        raise SourceFormatError(issues)

    required_headers = _required_headers(declared)
    reader = csv.reader(io.StringIO("\n".join(lines[column_line - 1 :])))
    try:
        columns = next(reader)
    except StopIteration:
        columns = []
    if columns != required_headers:
        issues.append(
            ParseIssue(
                "error",
                "BAD_HEADER",
                column_line,
                None,
                f"expected columns {required_headers}, received {columns}",
            )
        )
        raise SourceFormatError(issues)

    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for line_number, row in enumerate(reader, start=column_line + 1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) != len(columns):
            issues.append(
                ParseIssue(
                    "error",
                    "BAD_COLUMN_COUNT",
                    line_number,
                    None,
                    f"expected {len(columns)} columns, received {len(row)}",
                )
            )
            continue
        record = {column: cell for column, cell in zip(columns, row)}
        record_id = record.get("id", "").strip()
        if not record_id:
            issues.append(ParseIssue("error", "MISSING_ID", line_number, None, "record id is required"))
            continue
        if record_id in seen:
            issues.append(ParseIssue("error", "DUPLICATE_ID", line_number, record_id, f"duplicate id {record_id}"))
            continue
        seen.add(record_id)

        if declared == "NMR-LIST":
            parsed_record = _parse_1d_record(record, line_number, record_id, headers, issues)
        else:
            parsed_record = _parse_2d_record(record, line_number, record_id, headers, issues)
        records.append(parsed_record)

    if issues:
        raise SourceFormatError(issues)
    _validate_required_metadata(declared, headers, issues)
    if issues:
        raise SourceFormatError(issues)

    kind = "1d" if declared == "NMR-LIST" else "2d"
    return ParsedSource(kind, headers.get("format_version", "unknown"), _normalize_headers(headers), records, None, issues)


def _read_header(line: str) -> str:
    stripped = line.strip()
    if stripped in {"# NMR-LIST v1", "# NMR-CORRELATIONS v1"}:
        return stripped.split()[1]
    raise SourceFormatError(
        [ParseIssue("error", "BAD_FORMAT_HEADER", 1, None, "first line must be '# NMR-LIST v1' or '# NMR-CORRELATIONS v1'")]
    )


def _read_metadata_line(line: str, line_number: int, issues: list[ParseIssue]) -> tuple[str | None, str]:
    body = line.lstrip("#").strip()
    if ":" not in body:
        issues.append(ParseIssue("error", "BAD_METADATA", line_number, None, f"invalid metadata: {body}"))
        return None, body
    key, value = body.split(":", 1)
    return key.strip(), value.strip()


def _required_headers(declared: str) -> list[str]:
    if declared == "NMR-LIST":
        return ["id", "shift_ppm", "intensity_rel", "integral_proton", "multiplicity", "couplings_hz", "notes"]
    return ["id", "from_shift_ppm", "to_shift_ppm", "intensity_rel", "notes"]


def _normalize_headers(headers: dict[str, str]) -> dict[str, object]:
    result: dict[str, object] = dict(headers)
    if "spectrometer_frequency" in headers:
        result["spectrometer_frequency"] = _measurement(headers["spectrometer_frequency"], FREQUENCY_RE, "MHz").as_json()
    if "temperature" in headers:
        result["temperature"] = _measurement(headers["temperature"], TEMPERATURE_RE, "K").as_json()
    return result


def _validate_required_metadata(declared: str, headers: dict[str, str], issues: list[ParseIssue]) -> None:
    required = {
        "NMR-LIST": ["type", "nucleus", "solvent", "temperature", "spectrometer_frequency", "reference"],
        "NMR-CORRELATIONS": ["type", "experiment", "from_nucleus", "to_nucleus", "solvent", "temperature", "spectrometer_frequency", "reference"],
    }[declared]
    for key in required:
        if not headers.get(key):
            issues.append(ParseIssue("error", "MISSING_METADATA", None, None, f"metadata {key} is required"))


def _parse_1d_record(
    record: dict[str, str], line_number: int, record_id: str, headers: dict[str, str], issues: list[ParseIssue]
) -> dict[str, object]:
    shift = _measurement(record["shift_ppm"], SHIFT_RE, "ppm", headers.get("reference"))
    intensity = _relative_number(record["intensity_rel"], "intensity", line_number, record_id, issues)
    integral = _relative_number(record["integral_proton"], "integral", line_number, record_id, issues)
    multiplicity_raw = record["multiplicity"].strip()
    multiplicity = {
        "raw": multiplicity_raw,
        "code": multiplicity_raw,
        "convertible": multiplicity_raw in {"s", "d", "t", "q", "sept", "m", "d/t", "d+t"},
    }
    if not multiplicity["convertible"]:
        multiplicity["diagnostic"] = "unknown multiplicity code retained at source position"
    couplings = _parse_couplings(record["couplings_hz"], line_number, record_id, issues)
    return {
        "id": record_id,
        "shift": shift.as_json(),
        "intensity": intensity,
        "integral": integral,
        "multiplicity": multiplicity,
        "couplings_hz": couplings,
        "notes": record["notes"],
        "source_line": line_number,
    }


def _parse_2d_record(
    record: dict[str, str], line_number: int, record_id: str, headers: dict[str, str], issues: list[ParseIssue]
) -> dict[str, object]:
    return {
        "id": record_id,
        "from_shift": _measurement(record["from_shift_ppm"], SHIFT_RE, "ppm", headers.get("reference")).as_json(),
        "to_shift": _measurement(record["to_shift_ppm"], SHIFT_RE, "ppm", headers.get("reference")).as_json(),
        "intensity": _relative_number(record["intensity_rel"], "intensity", line_number, record_id, issues),
        "notes": record["notes"],
        "source_line": line_number,
    }


def _measurement(raw: str, pattern: re.Pattern[str], unit: str, reference: str | None = None) -> Measurement:
    match = pattern.match(raw)
    if not match:
        return Measurement(raw=raw, value=None, unit=unit, coordinate_reference=reference, convertible=False, diagnostic="unconvertible measurement retained")
    value = float(match.group(1))
    parsed_unit = match.group(2) or unit
    if parsed_unit.lower() == "hz" and unit == "mhz":
        value /= 1_000_000.0
        parsed_unit = "MHz"
    return Measurement(raw=raw, value=value, unit=parsed_unit, coordinate_reference=reference)


def _relative_number(raw: str, field: str, line: int, record_id: str, issues: list[ParseIssue]) -> dict[str, object]:
    try:
        return {"raw": raw, "value": float(raw), "unit": "rel" if field == "intensity" else "proton", "convertible": True}
    except ValueError:
        issues.append(ParseIssue("diagnostic", "UNCONVERTIBLE_VALUE", line, record_id, f"{field} value is unconvertible and retained"))
        return {"raw": raw, "value": None, "unit": "rel" if field == "intensity" else "proton", "convertible": False, "diagnostic": "unconvertible value retained"}


def _parse_couplings(raw: str, line: int, record_id: str, issues: list[ParseIssue]) -> list[dict[str, object]]:
    if not raw.strip() or raw.strip() in {"-", "NA"}:
        return []
    values = []
    for part in raw.split(";"):
        try:
            values.append({"raw": part.strip(), "value": float(part), "unit": "Hz", "convertible": True})
        except ValueError:
            issues.append(ParseIssue("diagnostic", "UNCONVERTIBLE_COUPLING", line, record_id, "coupling value retained"))
            values.append({"raw": part.strip(), "value": None, "unit": "Hz", "convertible": False, "diagnostic": "unconvertible coupling retained"})
    return values


def parse_molecule_json(filename: str, content: str) -> ParsedSource:
    issues: list[ParseIssue] = []
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SourceFormatError([ParseIssue("error", "INVALID_JSON", exc.lineno, None, exc.msg)]) from exc
    if data.get("format_version") != "nmr-molecule/v1":
        issues.append(ParseIssue("error", "BAD_FORMAT_VERSION", None, None, "format_version must be nmr-molecule/v1"))
    for key in ["molecule", "atoms", "bonds", "expected_edges"]:
        if key not in data:
            issues.append(ParseIssue("error", "MISSING_KEY", None, None, f"{key} is required"))
    if issues:
        raise SourceFormatError(issues)
    atom_ids = {atom.get("id") for atom in data["atoms"]}
    for bond in data["bonds"]:
        if len(bond) != 2 or any(endpoint not in atom_ids for endpoint in bond):
            issues.append(ParseIssue("error", "BAD_BOND", None, None, f"invalid bond {bond}"))
    for edge in data["expected_edges"]:
        if edge.get("from_atom") not in atom_ids or edge.get("to_atom") not in atom_ids:
            issues.append(ParseIssue("error", "BAD_EXPECTED_EDGE", None, edge.get("id"), "unknown atom in expected edge"))
    if issues:
        raise SourceFormatError(issues)
    return ParsedSource("molecule", data["format_version"], {"molecule": data["molecule"]}, data["atoms"], data, [])
