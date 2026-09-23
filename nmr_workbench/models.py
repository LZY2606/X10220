from dataclasses import dataclass
from typing import Any, Literal

Verdict = Literal["accepted", "rejected", "deferred", "withdrawn"]
CandidateLifecycle = Literal["legal", "rejected", "unknown"]


@dataclass(frozen=True)
class ImportDiagnostic:
    code: str
    message: str
    path: str
    value_id: str | None = None
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "value_id": self.value_id,
            "severity": self.severity,
        }


class ImportValidationError(ValueError):
    def __init__(self, diagnostics: list[ImportDiagnostic]):
        super().__init__("; ".join(d.message for d in diagnostics))
        self.diagnostics = diagnostics
