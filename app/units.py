"""Quantity handling: keep the original representation, only the derived
layer normalises. Values that cannot be converted stay in place with a
diagnostic instead of being dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

ACCEPTED_UNITS = {
    "shift": {"ppm"},
    "intensity": {"a.u.", "counts", ""},
    "integral": {"H", "rel"},
    "temperature": {"K", "C"},
    "frequency": {"MHz"},
}


@dataclass
class Quantity:
    value: Optional[float]
    unit: str
    raw: str
    diagnostics: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "raw": self.raw,
            "diagnostics": list(self.diagnostics),
        }


def parse_quantity(raw: str, kind: str, default_unit: str = "") -> Quantity:
    text = (raw or "").strip()
    diagnostics: List[str] = []
    if not text:
        return Quantity(None, default_unit, text, ["empty value"])
    parts = text.split(None, 1)
    num_text = parts[0]
    unit = parts[1].strip() if len(parts) > 1 else default_unit
    try:
        value = float(num_text)
    except ValueError:
        q = Quantity(None, unit, text, [f"non-numeric {kind} value: {num_text!r}"])
        return q
    allowed = ACCEPTED_UNITS.get(kind, set())
    if allowed and unit not in allowed:
        diagnostics.append(
            f"unit {unit!r} not recognised for {kind}; original kept at source"
        )
    return Quantity(value, unit, text, diagnostics)


def to_kelvin(q: Quantity) -> Optional[float]:
    if q.value is None:
        return None
    if q.unit == "K":
        return q.value
    if q.unit == "C":
        return q.value + 273.15
    return None


def quantity_from_dict(data: dict) -> Quantity:
    return Quantity(
        value=data.get("value"),
        unit=data.get("unit", ""),
        raw=data.get("raw", ""),
        diagnostics=list(data.get("diagnostics", [])),
    )
