"""Domain model. Everything carries a stable identity; raw text is kept
alongside normalized values."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .units import Quantity, Multiplicity

EXPERIMENTS_1D = {"1H", "13C"}
EXPERIMENTS_2D = {"COSY", "HSQC", "HMBC"}
NUCLEUS_OF = {"1H": "H", "13C": "C", "COSY": "H", "HSQC": "CH", "HMBC": "CH"}


@dataclass
class Conditions:
    solvent: str = ""
    frequency_mhz: Optional[float] = None
    temperature_K: Optional[float] = None
    reference: str = ""

    def to_dict(self):
        return {
            "solvent": self.solvent,
            "frequency_mhz": self.frequency_mhz,
            "temperature_K": self.temperature_K,
            "reference": self.reference,
        }


@dataclass
class Peak:
    id: str
    source_id: str
    experiment: str
    nucleus: str
    shift: Quantity
    intensity: Quantity
    integral: Quantity
    multiplicity: Multiplicity

    def to_dict(self):
        return {
            "id": self.id,
            "source_id": self.source_id,
            "experiment": self.experiment,
            "nucleus": self.nucleus,
            "shift": self.shift.to_dict(),
            "intensity": self.intensity.to_dict(),
            "integral": self.integral.to_dict(),
            "multiplicity": self.multiplicity.to_dict(),
        }


@dataclass
class Edge:
    id: str
    source_id: str
    kind: str  # COSY | HSQC | HMBC
    from_peak: str
    to_peak: str

    def to_dict(self):
        return {
            "id": self.id,
            "source_id": self.source_id,
            "kind": self.kind,
            "from_peak": self.from_peak,
            "to_peak": self.to_peak,
        }


@dataclass
class Atom:
    id: str
    element: str  # H | C
    label: str
    shift_min: float
    shift_max: float
    symmetry_group: str = ""
    x: float = 0.0
    y: float = 0.0
    attached_protons: int = 0

    def to_dict(self):
        return {
            "id": self.id,
            "element": self.element,
            "label": self.label,
            "shift_min": self.shift_min,
            "shift_max": self.shift_max,
            "symmetry_group": self.symmetry_group,
            "x": self.x,
            "y": self.y,
            "attached_protons": self.attached_protons,
        }


@dataclass
class Bond:
    a: str
    b: str

    def to_dict(self):
        return {"a": self.a, "b": self.b}


@dataclass
class ParsedDoc:
    kind: str  # "peaklist" | "correlation" | "molecule"
    experiment: str = ""
    conditions: Conditions = field(default_factory=Conditions)
    peaks: List[Peak] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    atoms: List[Atom] = field(default_factory=list)
    bonds: List[Bond] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
