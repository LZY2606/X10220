from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import PARSER_VERSION

_TOKEN_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|[^\s#]+)')


@dataclass
class ParsedDocument:
    kind: str
    records: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {"meta": [], "atoms": [], "bonds": [], "peaks": [], "edges": [], "raw_records": []})
    diagnostics: list[dict[str, str]] = field(default_factory=list)
    parser_version: str = PARSER_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "records": self.records, "diagnostics": self.diagnostics, "parser_version": self.parser_version}


def parse_source(filename: str, text: str) -> ParsedDocument:
    suffix = filename.lower().rsplit(".", 1)[-1]
    kind = {"mol": "molecule", "1d": "peak1d", "2d": "peak2d"}.get(suffix)
    if kind is None:
        raise ValueError(f"unsupported source extension: {filename}; use .mol, .1d, or .2d")
    doc = ParsedDocument(kind=kind)
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        head, sep, body = stripped.partition(" ")
        values = _parse_values(body if sep else "")
        values["record_type"] = head.lower()
        values["line"] = line_number
        values["raw_text"] = stripped
        if head.isupper() and head.isidentifier():
            _record(doc, kind, head, values, seen)
        else:
            _diagnostic(doc, line_number, stripped, "unknown_record", "record was retained but is not used in derived calculations")
            doc.records["raw_records"].append(values)
    if not doc.records["meta"]:
        raise ValueError(f"{filename}: missing META record")
    return doc


def _parse_values(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in _TOKEN_RE.findall(text):
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"')
        result[key] = value
    return result


def _record(doc: ParsedDocument, source_kind: str, head: str, values: dict[str, Any], seen: set[tuple[str, str]]) -> None:
    line = int(values["line"])
    raw = str(values["raw_text"])
    record_id = values.get("id")
    if head in {"MOL", "META"}:
        doc.records["meta"].append(values)
        return
    if not record_id:
        _diagnostic(doc, line, raw, "missing_id", "retained as raw record because id is required")
        doc.records["raw_records"].append(values)
        return
    bucket = {"ATOM": "atoms", "BOND": "bonds", "PEAK": "peaks", "EDGE": "edges"}.get(head)
    if bucket is None:
        _diagnostic(doc, line, raw, "unknown_record", "retained as raw record")
        doc.records["raw_records"].append(values)
        return
    key = (bucket, record_id)
    if key in seen:
        _diagnostic(doc, line, raw, "duplicate_id", f"duplicate {bucket} id {record_id}; retained as raw record")
        doc.records["raw_records"].append(values)
        return
    seen.add(key)
    required = _required(head, source_kind)
    missing = [name for name in required if name not in values]
    numeric_fields = {"x", "y", "shift", "intensity", "integral", "from_shift", "to_shift", "temperature_k", "spectrometer_mhz"}
    bad_numeric: list[str] = []
    normalized: dict[str, Any] = dict(values)
    for name in list(numeric_fields.intersection(normalized.keys())):
        try:
            normalized[name] = float(normalized[name])
        except ValueError:
            bad_numeric.append(name)
    if missing or bad_numeric:
        reason = []
        if missing:
            reason.append("missing: " + ",".join(missing))
        if bad_numeric:
            reason.append("unconvertible numeric: " + ",".join(bad_numeric))
        _diagnostic(doc, line, raw, "invalid_record", "; ".join(reason))
        normalized["usable"] = False
        doc.records["raw_records"].append(normalized)
        return
    normalized["usable"] = True
    doc.records[bucket].append(normalized)


def _required(head: str, source_kind: str) -> list[str]:
    if head == "ATOM":
        return ["element"]
    if head == "BOND":
        return ["from", "to"]
    if head == "PEAK":
        return ["shift", "shift_unit"]
    if head == "EDGE":
        if source_kind == "peak2d":
            return ["experiment", "from_shift", "from_shift_unit", "to_shift", "to_shift_unit"]
        return ["kind", "from", "to"]
    return []


def _diagnostic(doc: ParsedDocument, line: int, raw: str, code: str, message: str) -> None:
    doc.diagnostics.append({"line": str(line), "code": code, "message": message, "raw_text": raw})
