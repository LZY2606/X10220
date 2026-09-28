from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any

from .ids import record_id, source_version_id
from .quantities import make_acquisition_value, make_value


REQUIRED_COLUMNS: dict[str, list[str]] = {
    "structure": ["atom_id", "element", "expected_shift_ppm", "multiplicity"],
    "1H": ["peak_id", "shift_ppm", "intensity_counts", "integral_proton", "multiplicity"],
    "13C": ["peak_id", "shift_ppm", "intensity_counts", "multiplicity"],
    "COSY": ["edge_id", "from_peak_id", "to_peak_id"],
    "HSQC": ["edge_id", "proton_shift_ppm", "carbon_shift_ppm"],
    "HMBC": ["edge_id", "proton_shift_ppm", "carbon_shift_ppm"],
}

LOGICAL_ID_FIELD = {
    "structure": "atom_id",
    "1H": "peak_id",
    "13C": "peak_id",
    "COSY": "edge_id",
    "HSQC": "edge_id",
    "HMBC": "edge_id",
}


class ParseError(ValueError):
    def __init__(self, message: str, *, source_kind: str, details: Any | None = None):
        super().__init__(message)
        self.source_kind = source_kind
        self.details = details or {"message": message}


@dataclass
class ParseResult:
    source: dict[str, Any]
    diagnostics: list[dict[str, Any]]


def read_csv_rows(content: str, source_kind: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(content, newline=""))
    fields = reader.fieldnames or []
    missing = [column for column in REQUIRED_COLUMNS[source_kind] if column not in fields]
    if missing:
        raise ParseError(
            f"{source_kind} missing required columns: {', '.join(missing)}",
            source_kind=source_kind,
            details={"code": "missing_required_columns", "missing_columns": missing, "columns": fields},
        )
    return fields, list(reader)


def parse_csv_source(source_kind: str, source_id: str, filename: str, content: str, version_index: int) -> ParseResult:
    fields, rows = read_csv_rows(content, source_kind)
    records: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    logical_field = LOGICAL_ID_FIELD[source_kind]

    for zero_row, raw_row in enumerate(rows):
        row_number = zero_row + 2
        logical_id = (raw_row.get(logical_field) or "").strip()
        rec_id = record_id(source_id, logical_id, row_number)
        values = []
        invalid_fields = []
        for field in fields:
            value = make_value(source_id, logical_id or f"row-{row_number}", field, raw_row.get(field, ""), kind=source_kind)
            values.append(value)
            if not value["valid"]:
                invalid_fields.append(field)
                diagnostics.append(
                    {
                        "code": "unconvertible_value_retained",
                        "severity": "warning",
                        "source_id": source_id,
                        "record_id": rec_id,
                        "logical_id": logical_id,
                        "row": row_number,
                        "field": field,
                        "raw": value["raw"],
                        "message": value["diagnostic"],
                    }
                )
        record_type = "atom" if source_kind == "structure" else "edge" if source_kind in {"COSY", "HSQC", "HMBC"} else "peak"
        records.append(
            {
                "record_id": rec_id,
                "logical_id": logical_id,
                "record_type": record_type,
                "row": row_number,
                "values": values,
                "raw_row": raw_row,
                "invalid_fields": invalid_fields,
            }
        )

    version_id = source_version_id(source_id, content, filename)
    source = {
        "source_id": source_id,
        "source_kind": source_kind,
        "logical_name": source_kind,
        "filename": filename,
        "version_index": version_index,
        "version_id": version_id,
        "raw_content": content,
        "format": "csv",
        "fields": fields,
        "records": records,
        "diagnostics": diagnostics,
    }
    return ParseResult(source, diagnostics)


def parse_acquisition(source_id: str, filename: str, content: str, version_index: int) -> ParseResult:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ParseError(
            f"acquisition JSON is invalid at line {exc.lineno}, column {exc.colno}",
            source_kind="acquisition",
            details={"code": "invalid_json", "line": exc.lineno, "column": exc.colno, "message": exc.msg},
        ) from exc
    if not isinstance(data, dict):
        raise ParseError(
            "acquisition JSON root must be an object",
            source_kind="acquisition",
            details={"code": "invalid_root", "actual_type": type(data).__name__},
        )

    required = ["solvent", "temperature_kelvin", "reference_standard", "proton_frequency_mhz", "carbon_frequency_mhz"]
    missing = [field for field in required if field not in data]
    if missing:
        raise ParseError(
            f"acquisition missing required fields: {', '.join(missing)}",
            source_kind="acquisition",
            details={"code": "missing_required_fields", "missing_fields": missing},
        )

    values = []
    diagnostics = []
    for field in sorted(data.keys()):
        value = make_acquisition_value(source_id, field, data[field])
        values.append(value)
        if not value["valid"]:
            diagnostics.append(
                {
                    "code": "unconvertible_value_retained",
                    "severity": "warning",
                    "source_id": source_id,
                    "record_id": record_id(source_id, "acquisition"),
                    "logical_id": "acquisition",
                    "field": field,
                    "raw": value["raw"],
                    "message": value["diagnostic"],
                }
            )
    version_id = source_version_id(source_id, content, filename)
    source = {
        "source_id": source_id,
        "source_kind": "acquisition",
        "logical_name": "acquisition",
        "filename": filename,
        "version_index": version_index,
        "version_id": version_id,
        "raw_content": content,
        "format": "json",
        "fields": [value["field"] for value in values],
        "records": [
            {
                "record_id": record_id(source_id, "acquisition"),
                "logical_id": "acquisition",
                "record_type": "acquisition",
                "row": 1,
                "values": values,
                "raw_row": data,
                "invalid_fields": [item["field"] for item in diagnostics],
            }
        ],
        "diagnostics": diagnostics,
    }
    return ParseResult(source, diagnostics)


def parse_source(source_kind: str, source_id: str, filename: str, content: str, version_index: int = 1) -> ParseResult:
    if source_kind == "acquisition":
        return parse_acquisition(source_id, filename, content, version_index)
    if source_kind not in REQUIRED_COLUMNS:
        raise ParseError("unknown source kind", source_kind=source_kind, details={"code": "unknown_source_kind"})
    return parse_csv_source(source_kind, source_id, filename, content, version_index)


def value_map(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {value["field"]: value for value in record["values"]}


def parsed_records(source: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for record in source["records"]:
        values = value_map(record)
        item = {
            "record_id": record["record_id"],
            "logical_id": record["logical_id"],
            "row": record["row"],
            "invalid_fields": record["invalid_fields"],
        }
        for field, value in values.items():
            item[field] = value["parsed"]
            item[f"{field}__raw"] = value["raw"]
            item[f"{field}__value_id"] = value["value_id"]
        result.append(item)
    return result
