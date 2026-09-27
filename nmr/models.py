from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any


def stable_id(*parts: Any) -> str:
    return ":".join(str(part) for part in parts)


def canonical_fingerprint(value: Any) -> str:
    body = canonical_json(value).encode("utf-8")
    return sha256(body).hexdigest()


def canonical_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Measurement:
    raw: str
    value: float | None
    unit: str
    coordinate_reference: str | None = None
    convertible: bool = True
    diagnostic: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "coordinate_reference": self.coordinate_reference,
            "convertible": self.convertible,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class ParseIssue:
    severity: str
    code: str
    line: int | None
    object_id: str | None
    message: str

    def as_json(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "line": self.line,
            "object_id": self.object_id,
            "message": self.message,
        }


@dataclass
class ParsedSource:
    kind: str
    format_version: str
    headers: dict[str, Any]
    records: list[dict[str, Any]]
    molecule: dict[str, Any] | None
    issues: list[ParseIssue]

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "format_version": self.format_version,
            "headers": self.headers,
            "records": self.records,
            "molecule": self.molecule,
            "issues": [issue.as_json() for issue in self.issues],
        }

