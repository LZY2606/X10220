"""Domain quantities.

Every domain value keeps its *raw representation* (exactly as written in the
source file) together with a unit / coordinate basis. Normalised numeric
values live only in the derived layer; values that cannot be converted stay
at their original position and carry diagnostics instead of being dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any

_QUANTITY_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z%./0-9-]*)\s*$")

_DIMENSIONS: Dict[str, Dict[str, float]] = {
    "chemical_shift": {"ppm": 1.0},
    "frequency": {"MHz": 1.0, "Hz": 1.0e-6, "kHz": 1.0e-3, "GHz": 1.0e3},
    "integral": {"": 1.0, "H": 1.0, "count": 1.0},
    "intensity": {"": 1.0, "a.u.": 1.0, "%": 0.01},
    "temperature": {"K": 1.0, "C": 1.0, "degC": 1.0, "F": 5.0 / 9.0},
    "j_coupling": {"Hz": 1.0},
}


@dataclass
class Quantity:
    """A measured value with raw text, unit basis and optional normalisation."""

    raw: str
    value: Optional[float] = None
    unit: str = ""
    dimension: str = ""
    normalised: Optional[float] = None
    target_unit: str = ""
    diagnostic: Optional[str] = None

    def is_convertible(self) -> bool:
        return self.diagnostic is None and self.normalised is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_quantity(raw: str, dimension: str) -> Quantity:
    """Parse ``"1.23 ppm"``-style text for a known dimension.

    Unknown units never raise: the raw text is kept with a diagnostic so a
    plotting layer cannot silently swallow the value.
    """
    text = raw.strip()
    match = _QUANTITY_RE.match(text)
    bases = _DIMENSIONS.get(dimension)
    if match is None:
        return Quantity(raw=text, dimension=dimension,
                        diagnostic="not_a_numeric_quantity")
    value = float(match.group(1))
    unit = match.group(2)
    result = Quantity(raw=text, value=value, unit=unit, dimension=dimension)
    if bases is None:
        result.diagnostic = "unknown_dimension:%s" % dimension
        return result
    if unit not in bases:
        result.diagnostic = "unconvertible_unit:%s" % unit
        return result
    target_unit = next(iter(bases))
    converted = value * bases[unit]
    if dimension == "temperature" and unit in ("C", "degC"):
        converted = value + 273.15
    elif dimension == "temperature" and unit == "F":
        converted = (value - 32.0) * 5.0 / 9.0 + 273.15
    result.normalised = converted
    result.target_unit = target_unit
    return result


def normalised_shift(raw: str) -> Optional[float]:
    parsed = parse_quantity(raw, "chemical_shift")
    return parsed.normalised


def format_quantity(q: Dict[str, Any]) -> str:
    return str(q.get("raw", ""))
