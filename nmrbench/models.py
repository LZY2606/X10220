"""Structural domain records produced by the import layer.

All records carry a stable identity (``id``) plus the source document and the
raw line number so every displayed value can be traced back to the original
file and version.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .units import ValueCell, Quantity


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_json(payload: Any) -> str:
    """Deterministic content fingerprint for derived inputs (params, etc.)."""
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "fp:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


@dataclass
class Peak:
    """One row of a 1D peak list."""

    id: str                      # fully qualified stable id
    local_id: str                # id from the file (or generated PK001)
    source_id: str
    version: int
    line_no: int
    nucleus: str                 # "1H" / "13C"
    shift: ValueCell             # chemical_shift ppm
    intensity: ValueCell         # arbitrary relative intensity unit
    integral: Optional[ValueCell]
    multiplicity: Optional[str]  # raw token, e.g. "d" / "q" / "m" / "dd"
    j_couplings: list[ValueCell] = field(default_factory=list)
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "local_id": self.local_id,
            "source_id": self.source_id,
            "version": self.version,
            "line_no": self.line_no,
            "nucleus": self.nucleus,
            "shift": self.shift.to_dict(),
            "intensity": self.intensity.to_dict(),
            "integral": self.integral.to_dict() if self.integral else None,
            "multiplicity": self.multiplicity,
            "j_couplings": [j.to_dict() for j in self.j_couplings],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Peak":
        return cls(
            id=data["id"], local_id=data["local_id"], source_id=data["source_id"],
            version=data["version"], line_no=data["line_no"], nucleus=data["nucleus"],
            shift=ValueCell.from_dict(data["shift"]),
            intensity=ValueCell.from_dict(data["intensity"]),
            integral=ValueCell.from_dict(data["integral"]) if data.get("integral") else None,
            multiplicity=data.get("multiplicity"),
            j_couplings=[ValueCell.from_dict(j) for j in data.get("j_couplings", [])],
            notes=data.get("notes"),
        )


@dataclass
class CorrelationEdge:
    """One COSY / HSQC / HMBC correlation between two peaks.

    References are kept in their raw textual form and later resolved against
    the registry of peak ids (qualified ``source:version:local`` and bare
    ``local`` forms are both accepted).
    """

    id: str
    source_id: str
    version: int
    line_no: int
    kind: str                    # cosy | hsqc | hmbc | noesy
    from_ref: str
    to_ref: str
    from_peak: Optional[str] = None
    to_peak: Optional[str] = None
    strength: Optional[ValueCell] = None
    diagnostic: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CorrelationEdge":
        return cls(
            id=data["id"], source_id=data["source_id"], version=data["version"],
            line_no=data["line_no"], kind=data["kind"], from_ref=data["from_ref"],
            to_ref=data["to_ref"], from_peak=data.get("from_peak"),
            to_peak=data.get("to_peak"),
            strength=ValueCell.from_dict(data["strength"]) if data.get("strength") else None,
            diagnostic=data.get("diagnostic"),
        )


@dataclass
class Atom:
    """One nucleus node in the molecular skeleton."""

    id: str
    element: str                # H / C
    attached_to: Optional[str] = None  # carbon id for a proton
    equivalent_group: Optional[str] = None
    multiplicity: str = "u"     # H: 1H s/d/..., C: CH3/CH2/CH/Q
    predicted_shift: Optional[ValueCell] = None
    label: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "element": self.element, "attached_to": self.attached_to,
            "equivalent_group": self.equivalent_group, "multiplicity": self.multiplicity,
            "predicted_shift": self.predicted_shift.to_dict() if self.predicted_shift else None,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Atom":
        return cls(
            id=data["id"], element=data["element"], attached_to=data.get("attached_to"),
            equivalent_group=data.get("equivalent_group"), multiplicity=data.get("multiplicity", "u"),
            predicted_shift=ValueCell.from_dict(data["predicted_shift"]) if data.get("predicted_shift") else None,
            label=data.get("label"),
        )


@dataclass
class Conditions:
    """Acquisition / referencing conditions from a *.cond file."""

    source_id: str
    version: int
    solvent: Optional[str] = None
    temperature: Optional[ValueCell] = None
    reference: Optional[str] = None
    spectrometer_frequency: Optional[ValueCell] = None  # MHz
    note: Optional[str] = None

    def content_digest(self) -> dict[str, Any]:
        return {
            "solvent": self.solvent,
            "temperature": self.temperature.to_dict() if self.temperature else None,
            "reference": self.reference,
            "spectrometer_frequency": (
                self.spectrometer_frequency.to_dict() if self.spectrometer_frequency else None
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id, "version": self.version, "solvent": self.solvent,
            "temperature": self.temperature.to_dict() if self.temperature else None,
            "reference": self.reference,
            "spectrometer_frequency": (
                self.spectrometer_frequency.to_dict() if self.spectrometer_frequency else None
            ),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Conditions":
        return cls(
            source_id=data["source_id"], version=data["version"], solvent=data.get("solvent"),
            temperature=ValueCell.from_dict(data["temperature"]) if data.get("temperature") else None,
            reference=data.get("reference"),
            spectrometer_frequency=(
                ValueCell.from_dict(data["spectrometer_frequency"])
                if data.get("spectrometer_frequency") else None
            ),
            note=data.get("note"),
        )


@dataclass
class Skeleton:
    atoms: list[Atom] = field(default_factory=list)
    bonds: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"atoms": [a.to_dict() for a in self.atoms], "bonds": [list(b) for b in self.bonds]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Skeleton":
        return cls(
            atoms=[Atom.from_dict(a) for a in data["atoms"]],
            bonds=[tuple(b) for b in data.get("bonds", [])],
        )


@dataclass
class ParsedDocument:
    """Result of parsing one submitted raw file."""

    source_id: str
    kind: str                    # peaks-1H | peaks-13C | cosy | hsqc | hmbc | conditions | skeleton
    version: int
    filename: str
    raw_content: str
    raw_hash: str
    nuclei: list[str] = field(default_factory=list)
    peaks: list[Peak] = field(default_factory=list)
    edges: list[CorrelationEdge] = field(default_factory=list)
    conditions: Optional[Conditions] = None
    skeleton: Optional[Skeleton] = None
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    conditions_ref: Optional[str] = None  # $conditions key declared by a peak file

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id, "kind": self.kind, "version": self.version,
            "filename": self.filename, "raw_content": self.raw_content, "raw_hash": self.raw_hash,
            "nuclei": self.nuclei, "peaks": [p.to_dict() for p in self.peaks],
            "edges": [e.to_dict() for e in self.edges],
            "conditions": self.conditions.to_dict() if self.conditions else None,
            "skeleton": self.skeleton.to_dict() if self.skeleton else None,
            "diagnostics": self.diagnostics, "conditions_ref": self.conditions_ref,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParsedDocument":
        return cls(
            source_id=data["source_id"], kind=data["kind"], version=data["version"],
            filename=data["filename"], raw_content=data["raw_content"], raw_hash=data["raw_hash"],
            nuclei=list(data.get("nuclei", [])),
            peaks=[Peak.from_dict(p) for p in data.get("peaks", [])],
            edges=[CorrelationEdge.from_dict(e) for e in data.get("edges", [])],
            conditions=Conditions.from_dict(data["conditions"]) if data.get("conditions") else None,
            skeleton=Skeleton.from_dict(data["skeleton"]) if data.get("skeleton") else None,
            diagnostics=list(data.get("diagnostics", [])),
            conditions_ref=data.get("conditions_ref"),
        )


DEFAULT_PARAMS: dict[str, Any] = {
    "algorithm_version": "nmr-assign-1.0.0",
    "proton_shift_tolerance_ppm": 0.25,
    "carbon_shift_tolerance_ppm": 2.5,
    "tie_tolerance_ppm": 0.02,
    "weight_shift": 0.55,
    "weight_hsqc": 0.20,
    "weight_cosy": 0.15,
    "weight_hmbc": 0.10,
    "bias_threshold_ppm": 0.20,
    "min_evidence_count": 1,
}
