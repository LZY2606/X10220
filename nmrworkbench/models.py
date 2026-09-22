"""Domain dataclasses for the NMR assignment evidence workbench.

Identity rule: every displayed value keeps the id it received when first
parsed/created.  Import -> export -> delete DB -> import preserves ids, order
and provenance, so nothing here is auto-renamed during replay.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .units import Quantity


# --------------------------------------------------------------------------
# Imported source entities
# --------------------------------------------------------------------------
@dataclass
class Peak:
    id: str
    nucleus: str            # "1H" / "13C"
    shift: Quantity
    intensity: Quantity
    integration: Optional[Quantity]
    multiplicity: Optional[str]       # raw multiplicity token (q, d, dd...)
    multiplicity_norm: Optional[str]  # canonical form, None if unrecognised
    overlap_group: Optional[str]      # explicit overlap id; not forced unique
    meta: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["shift"] = self.shift.to_dict()
        d["intensity"] = self.intensity.to_dict()
        d["integration"] = self.integration.to_dict() if self.integration else None
        return d

    @staticmethod
    def from_dict(d: dict) -> "Peak":
        return Peak(
            id=d["id"], nucleus=d["nucleus"],
            shift=Quantity.from_dict(d["shift"]),
            intensity=Quantity.from_dict(d["intensity"]),
            integration=Quantity.from_dict(d["integration"]) if d.get("integration") else None,
            multiplicity=d.get("multiplicity"),
            multiplicity_norm=d.get("multiplicity_norm"),
            overlap_group=d.get("overlap_group"),
            meta=d.get("meta", {}),
        )


@dataclass
class Edge:
    id: str
    kind: str             # COSY / HSQC / HMBC
    from_peak: str
    to_peak: str
    strength: Quantity
    meta: Dict[str, str] = field(default_factory=dict)
    diagnostic: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["strength"] = self.strength.to_dict()
        return d

    @staticmethod
    def from_dict(d: dict) -> "Edge":
        return Edge(id=d["id"], kind=d["kind"], from_peak=d["from_peak"],
                    to_peak=d["to_peak"], strength=Quantity.from_dict(d["strength"]),
                    meta=d.get("meta", {}), diagnostic=d.get("diagnostic"))


@dataclass
class Site:
    """A nucleus site on the molecular skeleton."""
    id: str
    nucleus: str
    label: str
    expected_shift: float               # ppm on reference standard
    expected_mult: Optional[str]
    expected_integral: Optional[float] # H count; None for carbons
    symmetry_group: Optional[str]      # sites sharing this are equivalent
    x: float = 0.0
    y: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Site":
        return Site(**{k: d.get(k) for k in
                       ("id", "nucleus", "label", "expected_shift", "expected_mult",
                        "expected_integral", "symmetry_group", "x", "y")})


@dataclass
class Bond:
    site_a: str
    site_b: str
    order: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Bond":
        return Bond(d["site_a"], d["site_b"], d.get("order", 1))


@dataclass
class ExpectedEdge:
    """Skeleton-derived correlation expectation: used only as evidence."""
    id: str
    kind: str
    from_site: str
    to_site: str
    bond_distance: int

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ExpectedEdge":
        return ExpectedEdge(**d)


@dataclass
class Molecule:
    id: str
    name: str
    smiles: str
    sites: List[Site]
    bonds: List[Bond]
    expected_edges: List[ExpectedEdge] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "smiles": self.smiles,
            "sites": [s.to_dict() for s in self.sites],
            "bonds": [b.to_dict() for b in self.bonds],
            "expected_edges": [e.to_dict() for e in self.expected_edges],
        }

    @staticmethod
    def from_dict(d: dict) -> "Molecule":
        return Molecule(id=d["id"], name=d["name"], smiles=d.get("smiles", ""),
                        sites=[Site.from_dict(s) for s in d["sites"]],
                        bonds=[Bond.from_dict(b) for b in d["bonds"]],
                        expected_edges=[ExpectedEdge.from_dict(e)
                                        for e in d.get("expected_edges", [])])


@dataclass
class ExperimentMeta:
    solvent: str
    temperature: Optional[Quantity]
    reference_standard: str
    frequencies: Dict[str, Quantity]   # nucleus -> instrument frequency
    raw: Dict[str, str]                # original tokens, by field
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "solvent": self.solvent,
            "temperature": self.temperature.to_dict() if self.temperature else None,
            "reference_standard": self.reference_standard,
            "frequencies": {k: q.to_dict() for k, q in self.frequencies.items()},
            "raw": self.raw, "diagnostics": self.diagnostics,
        }

    @staticmethod
    def from_dict(d: dict) -> "ExperimentMeta":
        return ExperimentMeta(
            solvent=d["solvent"],
            temperature=Quantity.from_dict(d["temperature"]) if d.get("temperature") else None,
            reference_standard=d["reference_standard"],
            frequencies={k: Quantity.from_dict(v) for k, v in d.get("frequencies", {}).items()},
            raw=d.get("raw", {}), diagnostics=d.get("diagnostics", []))


# --------------------------------------------------------------------------
# Parameters / algorithm
# --------------------------------------------------------------------------
@dataclass
class Parameters:
    algorithm_version: str
    shift_tolerance_ppm_h: float = 0.50
    shift_tolerance_ppm_c: float = 2.00
    bias_correction: bool = True
    tie_epsilon: float = 0.02
    min_score: float = 0.55
    multiplicity_weight: float = 0.18
    integral_weight: float = 0.08
    hsqc_weight: float = 0.18
    cosy_weight: float = 0.12
    hmbc_weight: float = 0.12

    def fingerprint(self) -> str:
        import hashlib
        payload = {k: v for k, v in sorted(asdict(self).items())}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Parameters":
        allowed = {f for f in Parameters.__dataclass_fields__}
        return Parameters(**{k: v for k, v in d.items() if k in allowed})


# --------------------------------------------------------------------------
# Candidates (derived, first-class status/reasons)
# --------------------------------------------------------------------------
@dataclass
class RejectReason:
    code: str
    message: str
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "RejectReason":
        return RejectReason(**d)


@dataclass
class Candidate:
    id: str
    site_id: str
    nucleus: str
    peak_id: Optional[str]               # None => site has no observed peak (missing)
    status: str                          # legal / tied / rejected / unknown
    score: Optional[float]
    rank: int
    residual_ppm: Optional[float]
    components: Dict[str, float]         # evidence component scores
    evidence_edges: List[str]
    reasons: List[RejectReason]
    bias_applied_ppm: float = 0.0
    overlap: bool = False
    symmetry_ambiguous: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reasons"] = [r.to_dict() for r in self.reasons]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Candidate":
        return Candidate(
            id=d["id"], site_id=d["site_id"], nucleus=d["nucleus"],
            peak_id=d.get("peak_id"), status=d["status"], score=d.get("score"),
            rank=d["rank"], residual_ppm=d.get("residual_ppm"),
            components=d.get("components", {}), evidence_edges=d.get("evidence_edges", []),
            reasons=[RejectReason.from_dict(r) for r in d.get("reasons", [])],
            bias_applied_ppm=d.get("bias_applied_ppm", 0.0),
            overlap=d.get("overlap", False),
            symmetry_ambiguous=d.get("symmetry_ambiguous", False))


@dataclass
class CandidateRun:
    id: str
    params: Parameters
    param_fingerprint: str
    basis_revision: str                  # content-set hash used as dependency key
    candidates: List[Candidate]
    bias_estimate: Dict[str, float]
    diagnostics: List[str]
    algorithm_version: str
    basis_versions: Dict[str, int] = field(default_factory=dict)
    candidate_refs: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "params": self.params.to_dict(),
                "param_fingerprint": self.param_fingerprint,
                "basis_revision": self.basis_revision,
                "candidates": [c.to_dict() for c in self.candidates],
                "bias_estimate": self.bias_estimate, "diagnostics": self.diagnostics,
                "algorithm_version": self.algorithm_version}

    @staticmethod
    def from_dict(d: dict) -> "CandidateRun":
        return CandidateRun(id=d["id"], params=Parameters.from_dict(d["params"]),
                            param_fingerprint=d["param_fingerprint"],
                            basis_revision=d["basis_revision"],
                            candidates=[Candidate.from_dict(c) for c in d["candidates"]],
                            bias_estimate=d.get("bias_estimate", {}),
                            diagnostics=d.get("diagnostics", []),
                            algorithm_version=d.get("algorithm_version", d["params"]["algorithm_version"]),
                            basis_versions=d.get("basis_versions", {}),
                            candidate_refs=d.get("candidate_refs", {}))


# --------------------------------------------------------------------------
# Human decisions
# --------------------------------------------------------------------------
@dataclass
class Verdict:
    id: str
    site_id: str
    nucleus: str
    decision: str            # accepted / rejected
    peak_id: Optional[str]
    editor: str
    reason: str
    based_on_run: str
    retracted: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Verdict":
        allowed = {f for f in Verdict.__dataclass_fields__}
        return Verdict(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class Conflict:
    id: str
    site_id: str
    nucleus: str
    branch_a: str
    branch_b: str
    verdict_a: str
    verdict_b: str
    status: str              # open / resolved
    resolution: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Conflict":
        allowed = {f for f in Conflict.__dataclass_fields__}
        return Conflict(**{k: v for k, v in d.items() if k in allowed})
