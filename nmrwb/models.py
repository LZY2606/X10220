"""Core data structures for sources, rules, decisions and findings.

Persistence boundaries use plain JSON-serialisable dicts so SQLite, export
bundles and the API share one representation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# ---- enums (plain strings, shared with the UI) ----

SPECTRUM_1H = "1H"
SPECTRUM_13C = "13C"
KIND_1D = "1d"
KIND_COSY = "cosy"
KIND_HSQC = "hsqc"
KIND_HMBC = "hmbc"
TWO_D_KINDS = (KIND_COSY, KIND_HSQC, KIND_HMBC)

CAND_VIABLE = "viable"
CAND_REJECTED = "rejected"
CAND_UNKNOWN = "unknown"

PEAK_OK = "ok"
PEAK_UNCONVERTIBLE = "unconvertible"

DECISION_ACCEPT = "accept"
DECISION_REJECT = "reject"
DECISION_PARAM = "param"
DECISION_RESOLVE = "resolve"
DECISION_RETRACT = "retract"
DECISION_CHECKOUT = "checkout"

# Finding severities
SEV_INFO = "info"
SEV_WARN = "warning"

FINDING_OVERLAP = "F-OVERLAP-AMBIGUOUS"
FINDING_SYMMETRY = "F-SYMMETRY-EQUIVALENT"
FINDING_MISSING_EDGE = "F-MISSING-EDGE"
FINDING_BIAS = "F-SHIFT-BIAS-DETECTED"
FINDING_TIE = "F-TIED-CANDIDATES"
FINDING_CONFLICT = "F-EDITOR-CONFLICT"
FINDING_CONDITION = "F-CONDITION-CHANGE-EXPIRES"
FINDING_PARAM = "F-PARAM-REVISION"
FINDING_LOCAL = "F-LOCAL-CHANGE-PROPAGATION"
FINDING_MISSING_PEAK = "F-MISSING-PEAK-UNKNOWN"


def finding(code: str, severity: str, message: str, affected: Optional[List[str]] = None,
            **extra: Any) -> Dict[str, Any]:
    """Machine-readable finding/event payload."""
    data: Dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
        "affected": affected or [],
    }
    data.update(extra)
    return data


@dataclass
class Diagnostic:
    level: str          # error | warning | info
    code: str
    message: str
    line: Optional[int] = None
    ref: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedDocument:
    doc_type: str       # spectrum | structure | rulepack
    kind: str           # 1d/cosy/... or structure/rulepack
    nucleus: Optional[str]
    header: Dict[str, str]
    peaks: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]
    atoms: List[Dict[str, Any]]
    bonds: List[Dict[str, Any]]
    equiv_groups: List[Dict[str, Any]]
    expected_shifts: Dict[str, float]
    params: Dict[str, str]
    conditions: Dict[str, str]
    diagnostics: List[Diagnostic] = field(default_factory=list)

    def has_errors(self) -> bool:
        return any(d.level == "error" for d in self.diagnostics)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "kind": self.kind,
            "nucleus": self.nucleus,
            "header": self.header,
            "peaks": self.peaks,
            "edges": self.edges,
            "atoms": self.atoms,
            "bonds": self.bonds,
            "equiv_groups": self.equiv_groups,
            "expected_shifts": self.expected_shifts,
            "params": self.params,
            "conditions": self.conditions,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }
