"""Unit / coordinate-basis handling.

Raw representations are always preserved. Normalization happens only in the
derived layer. Values that cannot be converted stay in place and get a
diagnostic attached; they are never silently dropped.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

SHIFT_UNITS = {"ppm": 1.0}
INTENSITY_UNITS = {"abs": 1.0, "rel": 1.0}

_NUMBER = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([a-zA-Z%]*)\s*$")


@dataclass
class Quantity:
    """A domain quantity: raw text + optional normalized value + basis."""
    raw: str
    value: Optional[float] = None
    unit: Optional[str] = None
    basis: str = ""
    diagnostic: Optional[str] = None

    def to_dict(self):
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "basis": self.basis,
            "diagnostic": self.diagnostic,
        }


def parse_quantity(raw: str, allowed_units, basis: str) -> Quantity:
    q = Quantity(raw=raw, basis=basis)
    m = _NUMBER.match(raw or "")
    if not m:
        q.diagnostic = f"unconvertible value {raw!r}: not a number; kept raw"
        return q
    value, unit = float(m.group(1)), (m.group(2) or "").lower()
    if unit and unit not in allowed_units:
        q.diagnostic = (
            f"unconvertible unit {unit!r} in {raw!r}; kept raw, "
            f"expected one of {sorted(allowed_units)}"
        )
        return q
    q.value = value
    q.unit = unit or next(iter(allowed_units))
    return q


def parse_shift(raw: str) -> Quantity:
    return parse_quantity(raw, SHIFT_UNITS, basis="chemical shift scale")


def parse_intensity(raw: str) -> Quantity:
    return parse_quantity(raw, INTENSITY_UNITS, basis="detector intensity")


def parse_integral(raw: str) -> Quantity:
    return parse_quantity(raw, {"rel": 1.0}, basis="proton count (relative)")


MULTIPLICITIES = {"s", "d", "t", "q", "quint", "sext", "m", "dd", "dt", "br"}


@dataclass
class Multiplicity:
    raw: str
    code: Optional[str] = None
    diagnostic: Optional[str] = None

    def to_dict(self):
        return {"raw": self.raw, "code": self.code, "diagnostic": self.diagnostic}


def parse_multiplicity(raw: str) -> Multiplicity:
    code = (raw or "").strip().lower()
    mp = Multiplicity(raw=raw)
    if code in MULTIPLICITIES:
        mp.code = code
    else:
        mp.diagnostic = f"unknown multiplicity {raw!r}; kept raw"
    return mp
