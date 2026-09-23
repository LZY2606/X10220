"""Domain quantities.

Every domain number in the workbench carries a unit or coordinate basis.
Parsers keep the *raw* textual representation alongside the normalized value;
values that cannot be converted stay at their original location with a
diagnostic instead of being silently dropped (see ``ValueCell``).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# Unit registry keyed by (kind, symbol). Conversion factors map the symbol to a
# canonical unit of that kind.
UNITS: dict[tuple[str, str], dict[str, Any]] = {
    ("chemical_shift", "ppm"): {"canonical": "ppm", "factor": 1.0, "kind": "chemical_shift"},
    ("frequency", "MHz"): {"canonical": "MHz", "factor": 1.0, "kind": "frequency"},
    ("frequency", "Hz"): {"canonical": "Hz", "factor": 1.0, "kind": "frequency"},
    ("frequency", "kHz"): {"canonical": "Hz", "factor": 1000.0, "kind": "frequency"},
    ("splitting", "Hz"): {"canonical": "Hz", "factor": 1.0, "kind": "splitting"},
    ("splitting", "ppm"): {"canonical": "ppm", "factor": 1.0, "kind": "splitting"},
    ("temperature", "K"): {"canonical": "K", "factor": 1.0, "kind": "temperature"},
    ("temperature", "C"): {"canonical": "K", "factor": 1.0, "kind": "temperature"},
    ("intensity", "rel"): {"canonical": "rel", "factor": 1.0, "kind": "intensity"},
    ("integral", "rel"): {"canonical": "rel", "factor": 1.0, "kind": "integral"},
}


def normalize(value: float, kind: str, unit: Optional[str]) -> tuple[float, str]:
    """Convert ``value`` to the canonical unit of ``kind``.

    Celsius is handled separately (offset, not a scale factor). Returns the
    normalized value and canonical unit.
    """
    if unit is None or unit == "":
        raise ValueError(f"missing unit for kind {kind}")
    spec = UNITS.get((kind, unit))
    if spec is None:
        raise ValueError(f"unknown unit {unit!r} for kind {kind}")
    if kind == "temperature" and unit == "C":
        return value + 273.15, "K"
    return value * spec["factor"], spec["canonical"]


@dataclass
class ValueCell:
    """A single domain number with raw text, unit/basis and a normalized value.

    ``value`` is None when the raw text could not be converted; the cell stays
    in place and carries a diagnostic rather than being dropped.
    """

    raw: str
    kind: str
    unit: Optional[str] = None
    value: Optional[float] = None
    canonical_unit: Optional[str] = None
    basis: Optional[str] = None  # coordinate basis, e.g. "TMS" / "residual-solvent"
    diagnostic: Optional[str] = None

    @classmethod
    def parse(
        cls,
        raw: Any,
        kind: str,
        unit: Optional[str] = None,
        basis: Optional[str] = None,
    ) -> "ValueCell":
        text = "" if raw is None else str(raw).strip()
        cell = cls(raw=text, kind=kind, unit=unit, basis=basis)
        if text == "":
            cell.diagnostic = "empty value"
            return cell
        try:
            num = float(text)
        except ValueError:
            cell.diagnostic = f"not-a-number: {text!r}"
            return cell
        try:
            norm, canon = normalize(num, kind, unit)
        except ValueError as exc:
            cell.diagnostic = str(exc)
            return cell
        cell.value = norm
        cell.canonical_unit = canon
        return cell

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValueCell":
        return cls(**data)


@dataclass
class Quantity:
    """A numeric quantity with a fixed unit/basis (used for derived values)."""

    value: float
    unit: str
    basis: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Quantity":
        return cls(**data)
