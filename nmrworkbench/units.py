"""Quantities that always carry a unit / coordinate reference.

Import keeps the *raw* representation; only derived layers normalise.  Values
that cannot be converted stay in place with a diagnostic instead of being
silently dropped (and therefore never vanish just to make a drawing work).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Normalisation targets.  Frequencies are always MHz; chemical shifts are
# dimensionless but carry their reference standard explicitly elsewhere.
_UNIT_ALIASES = {
    "ppm": ("ppm", 1.0),
    "hz": ("Hz", 1.0),
    "mhz": ("MHz", 1.0),
    "a.u.": ("a.u.", 1.0),
    "au": ("a.u.", 1.0),
    "k": ("K", 1.0),
    "c": ("C", 1.0),
    "°c": ("C", 1.0),
    "": ("", 1.0),
}

_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


@dataclass(frozen=True)
class Quantity:
    raw: str                 # exact token as authored
    value: Optional[float]   # normalised numeric value, or None if unconvertible
    unit: str                # canonical unit, possibly "" if unspecified
    diagnostic: Optional[str] = None

    def as_ppm(self) -> Optional[float]:
        if self.value is None:
            return None
        if self.unit == "ppm" or self.unit == "":
            return self.value
        if self.unit == "Hz":
            return None  # needs instrument frequency; caller supplies convert_hz
        return None

    def to_dict(self) -> dict:
        return {"raw": self.raw, "value": self.value, "unit": self.unit,
                "diagnostic": self.diagnostic}

    @staticmethod
    def from_dict(d: dict) -> "Quantity":
        return Quantity(raw=d["raw"], value=d.get("value"), unit=d.get("unit", ""),
                        diagnostic=d.get("diagnostic"))


def parse_quantity(token: str, default_unit: str = "") -> Quantity:
    raw = token.strip()
    if raw == "":
        return Quantity(raw=raw, value=None, unit=default_unit,
                        diagnostic="empty token")
    m = re.match(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z.]*)$",
                 raw)
    if not m:
        return Quantity(raw=raw, value=None, unit=default_unit,
                        diagnostic=f"unparseable numeric representation {raw!r}")
    num_part, unit_part = m.group(1), m.group(2).strip()
    unit_key = unit_part.lower()
    if unit_key not in _UNIT_ALIASES:
        return Quantity(raw=raw, value=None, unit=unit_part,
                        diagnostic=f"unknown unit {unit_part!r}; value kept raw")
    canon, scale = _UNIT_ALIASES[unit_key]
    if canon == "" and default_unit:
        canon = default_unit
        if unit_key not in _UNIT_ALIASES:
            scale = 1.0
    return Quantity(raw=raw, value=float(num_part) * scale, unit=canon)


def parse_shift(token: str) -> Quantity:
    """Chemical shift token; bare numbers are interpreted as ppm."""
    return parse_quantity(token, default_unit="ppm")


def hz_to_ppm(hz: float, frequency_mhz: Optional[float]) -> tuple[Optional[float], Optional[str]]:
    if frequency_mhz in (None, 0.0):
        return None, "cannot convert Hz to ppm without instrument frequency"
    return hz / frequency_mhz, None
