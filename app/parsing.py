from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any


class ParseError(ValueError):
    """A fatal source-bundle parsing error."""


@dataclass(frozen=True)
class Quantity:
    raw_text: str
    value: float | None
    unit: str
    reference: str | None = None
    diagnostic: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "value": self.value,
            "unit": self.unit,
            "reference": self.reference,
            "diagnostic": self.diagnostic,
        }


def quantity(raw_text: Any, unit: str, reference: str | None = None) -> Quantity:
    text = "" if raw_text is None else str(raw_text).strip()
    if text == "":
        return Quantity(text, None, unit, reference, "empty value")
    try:
        return Quantity(text, float(text), unit, reference, None)
    except ValueError:
        return Quantity(text, None, unit, reference, f"cannot convert {text!r} to {unit}")


def _read_rows(raw_text: str) -> tuple[list[str], list[dict[str, str]]]:
    text = raw_text.lstrip("\ufeff")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        raise ParseError("CSV/TSV source has no header row")
    fields = [field.strip() for field in reader.fieldnames]
    rows: list[dict[str, str]] = []
    for row in reader:
        normalized = {(key or "").strip(): (value or "").strip() for key, value in row.items()}
        if any(normalized.values()):
            rows.append(normalized)
    return fields, rows


def parse_source_file(file_id: str, kind: str, version: str, raw_text: str) -> dict[str, Any]:
    normalized_kind = kind.strip().lower()
    parsed: dict[str, Any] = {
        "file_id": file_id,
        "kind": normalized_kind,
        "version": version,
        "raw_text": raw_text,
        "diagnostics": [],
    }
    if normalized_kind == "molecule":
        parsed["molecule"] = _parse_json(file_id, raw_text, "molecule")
    elif normalized_kind == "conditions":
        parsed["conditions"] = _parse_json(file_id, raw_text, "conditions")
    elif normalized_kind == "parameters":
        parsed["parameters"] = _parse_json(file_id, raw_text, "parameters")
    elif normalized_kind == "spectrum_1d":
        parsed.update(_parse_spectrum(file_id, raw_text))
    elif normalized_kind in {"cosy", "hsqc", "hmbc"}:
        parsed.update(_parse_correlation(file_id, raw_text, normalized_kind))
    else:
        raise ParseError(f"{file_id}: unsupported source kind {kind!r}")
    return parsed


def _parse_json(file_id: str, raw_text: str, expected: str) -> dict[str, Any]:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"{file_id}: invalid JSON: {exc.msg} at line {exc.lineno}") from exc
    if not isinstance(data, dict):
        raise ParseError(f"{file_id}: {expected} must be a JSON object")
    data["_raw_source_file"] = file_id
    return data


def _parse_spectrum(file_id: str, raw_text: str) -> dict[str, Any]:
    fields, rows = _read_rows(raw_text)
    required = {"peak_id", "nucleus", "shift", "intensity"}
    missing = sorted(required - set(fields))
    if missing:
        raise ParseError(f"{file_id}: missing required column(s): {', '.join(missing)}")
    peaks = []
    diagnostics = []
    for index, row in enumerate(rows, start=1):
        nucleus = row.get("nucleus", "").strip()
        conditions = _conditions_from_comments(row)
        shift = quantity(row.get("shift"), "ppm", row.get("reference") or "TMS")
        intensity = quantity(row.get("intensity"), "arbitrary units")
        integral = quantity(row.get("integral"), "equivalent nuclei") if row.get("integral") else None
        peak = {
            "peak_id": row.get("peak_id", "").strip(),
            "nucleus": nucleus,
            "shift": shift.as_dict(),
            "intensity": intensity.as_dict(),
            "integral": integral.as_dict() if integral else None,
            "multiplicity": row.get("multiplicity", ""),
            "assignment_hint": row.get("assignment_hint", ""),
            "source_file": file_id,
            "source_row": index,
            "raw_row": row,
            "conditions": conditions,
            "diagnostics": [],
        }
        if not peak["peak_id"]:
            diagnostic = f"row {index}: peak_id is empty"
            peak["diagnostics"].append(diagnostic)
            diagnostics.append(diagnostic)
        for name, measured in (("shift", shift), ("intensity", intensity)):
            if measured.value is None:
                diagnostic = f"row {index}: {name} {measured.diagnostic}"
                peak["diagnostics"].append(diagnostic)
                diagnostics.append(f"{file_id}: {diagnostic}")
        if integral and integral.value is None:
            diagnostic = f"row {index}: integral {integral.diagnostic}"
            peak["diagnostics"].append(diagnostic)
            diagnostics.append(f"{file_id}: {diagnostic}")
        peaks.append(peak)
    return {"fields": fields, "peaks": peaks, "diagnostics": diagnostics}


def _conditions_from_comments(row: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in row.items()
        if key not in {"peak_id", "nucleus", "shift", "intensity", "integral", "multiplicity", "assignment_hint", "reference"}
        and value
    }


def _parse_correlation(file_id: str, raw_text: str, kind: str) -> dict[str, Any]:
    fields, rows = _read_rows(raw_text)
    if kind == "cosy":
        required = {"edge_id", "from_peak", "to_peak"}
    else:
        required = {"edge_id", "proton_peak", "carbon_peak"}
    missing = sorted(required - set(fields))
    if missing:
        raise ParseError(f"{file_id}: missing required column(s): {', '.join(missing)}")
    edges = []
    diagnostics = []
    for index, row in enumerate(rows, start=1):
        if kind == "cosy":
            endpoints = [row.get("from_peak", ""), row.get("to_peak", "")]
        else:
            endpoints = [row.get("proton_peak", ""), row.get("carbon_peak", "")]
        coupling = quantity(row.get("coupling"), "Hz") if row.get("coupling") else None
        edge = {
            "edge_id": row.get("edge_id", "").strip(),
            "kind": kind.upper(),
            "endpoints": endpoints,
            "coupling": coupling.as_dict() if coupling else None,
            "intensity": quantity(row.get("intensity"), "arbitrary units").as_dict()
            if row.get("intensity")
            else None,
            "source_file": file_id,
            "source_row": index,
            "raw_row": row,
            "diagnostics": [],
        }
        if not edge["edge_id"] or not all(endpoints):
            diagnostic = f"row {index}: edge_id and both endpoints are required"
            edge["diagnostics"].append(diagnostic)
            diagnostics.append(f"{file_id}: {diagnostic}")
        edges.append(edge)
    return {"fields": fields, "edges": edges, "diagnostics": diagnostics}


def parse_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(bundle, dict):
        raise ParseError("bundle must be a JSON object")
    files = bundle.get("files")
    if not isinstance(files, list) or not files:
        raise ParseError("bundle.files must be a non-empty list")
    parsed_files = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(files, start=1):
        if not isinstance(item, dict):
            raise ParseError(f"bundle file #{index} must be an object")
        file_id = str(item.get("file_id", "")).strip()
        kind = str(item.get("kind", "")).strip()
        version = str(item.get("version", "v1")).strip()
        raw_text = item.get("raw_text")
        if not file_id or not kind:
            raise ParseError(f"bundle file #{index}: file_id and kind are required")
        if not isinstance(raw_text, str):
            raise ParseError(f"{file_id}: raw_text must be a string")
        key = (file_id, version)
        if key in seen:
            raise ParseError(f"duplicate immutable source {file_id} {version}")
        seen.add(key)
        parsed_files.append(parse_source_file(file_id, kind, version, raw_text))
    kinds = {item["kind"] for item in parsed_files}
    for required in ("molecule", "spectrum_1d", "conditions", "parameters", "cosy", "hsqc", "hmbc"):
        if required not in kinds:
            raise ParseError(f"bundle is missing at least one {required} source")
    return parsed_files
