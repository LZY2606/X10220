"""规范化后的领域对象（派生层）。

所有量都带单位或坐标基准；原始表示在导入层记录中保留。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Conditions:
    nucleus: str = ""
    solvent: str = ""
    temperature_k: Optional[float] = None
    temperature_repr: str = ""
    spectrometer_mhz: Optional[float] = None
    spectrometer_repr: str = ""
    reference: str = ""
    basis: str = "ppm-relative-to-reference"

    def signature(self) -> str:
        return f"{self.solvent}|{self.temperature_repr}|{self.reference}|{self.nucleus}"


@dataclass
class Peak1D:
    id: str
    label: Optional[str]
    doc_id: str
    doc_version: int
    lineno: int
    raw: str
    nucleus: str
    shift_ppm: Optional[float]
    shift_repr: str
    shift_unit: str
    intensity: Optional[float]
    intensity_repr: str
    intensity_unit: str
    integral: Optional[float]
    integral_repr: str
    integral_unit: str
    multiplicity: str
    conditions: Conditions

    def as_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["conditions"] = self.conditions.__dict__.copy()
        return d


@dataclass
class Edge2D:
    id: str
    label: Optional[str]
    doc_id: str
    doc_version: int
    lineno: int
    raw: str
    experiment: str          # COSY | HSQC | HMBC
    nuclei: List[str]
    f1_ppm: Optional[float]
    f2_ppm: Optional[float]
    f1_repr: str
    f2_repr: str
    f1_unit: str
    f2_unit: str
    strength: str
    strength_repr: str

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Site:
    id: str
    label: Optional[str]
    doc_id: str
    doc_version: int
    carbon: str
    hydrogen: str
    element: str
    expected_shift_ppm: Optional[float]
    expected_shift_repr: str
    expected_shift_unit: str
    symmetry_group: str
    protons: int = 1
    raw: str = ''

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ExpectedEdge:
    id: str
    kind: str            # bond (COSY) | hmbc
    a: str
    b: str
    edge_type: str


@dataclass
class Evidence:
    code: str
    weight: float
    contribution: float          # 0..1，证据支持度
    status: str                  # support | contradict | absent | unknown
    detail: str
    source_record_ids: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Candidate:
    site_id: str
    hydrogen: str
    carbon: str
    symmetry_group: str
    peak_id: Optional[str]           # None 表示“无法唯一决定/缺峰”的占位候选
    kind: str                        # assignment | unknown_unobserved
    score: float
    rank: int
    tied_rank: bool
    status: str                      # live | rejected | stale
    reject_reasons: List[str]
    evidences: List[Evidence]
    overlap: bool
    sources: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "site_id": self.site_id, "hydrogen": self.hydrogen, "carbon": self.carbon,
            "symmetry_group": self.symmetry_group, "peak_id": self.peak_id,
            "kind": self.kind, "score": round(self.score, 6), "rank": self.rank,
            "tied_rank": self.tied_rank, "status": self.status,
            "reject_reasons": self.reject_reasons,
            "evidences": [e.as_dict() for e in self.evidences],
            "overlap": self.overlap, "sources": self.sources,
        }


@dataclass
class Finding:
    code: str
    severity: str
    subject: str
    message: str
    related: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class AssignmentResult:
    id: str
    nucleus: str
    algo_version: str
    param_fingerprint: str
    params: Dict[str, Any]
    conditions_signature: str
    candidate_map: Dict[str, List[Candidate]]   # site_id -> 候选（按排名）
    findings: List[Finding]
    input_record_ids: Dict[str, List[str]]
    doc_versions: Dict[str, int]
    global_bias_ppm: Optional[float]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "nucleus": self.nucleus,
            "algo_version": self.algo_version,
            "param_fingerprint": self.param_fingerprint,
            "params": self.params,
            "conditions_signature": self.conditions_signature,
            "candidate_map": {k: [c.as_dict() for c in v] for k, v in self.candidate_map.items()},
            "findings": [f.as_dict() for f in self.findings],
            "input_record_ids": self.input_record_ids,
            "doc_versions": self.doc_versions,
            "global_bias_ppm": self.global_bias_ppm,
        }
