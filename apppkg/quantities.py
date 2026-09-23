"""Domain quantities.

Every physical value keeps its original textual representation, a parsed
numeric value when conversion is possible and an explicit unit / datum.
Values that cannot be converted stay in place with a diagnostic instead of
being silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Quantity:
    value: Optional[float]
    unit: str
    raw: str
    datum: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "raw": self.raw,
            "datum": self.datum,
            "error": self.error,
        }


def _number(token: str) -> float:
    token = token.strip()
    if token.lower() in {"nan", "inf", "-inf"}:
        raise ValueError("non-finite number")
    return float(token)


def parse_ppm(raw: str, isotope: str) -> Quantity:
    raw = (raw or "").strip()
    datum = f"chemical_shift_{isotope}_ppm"
    try:
        return Quantity(_number(raw), "ppm", raw, datum)
    except (ValueError, TypeError):
        return Quantity(None, "ppm", raw, datum, error="UNCONVERTIBLE_PPM")


def parse_frequency(raw: str) -> Quantity:
    raw = (raw or "").strip()
    parts = raw.split()
    try:
        value = _number(parts[0])
    except (ValueError, IndexError, TypeError):
        return Quantity(None, "MHz", raw, "spectrometer_frequency", error="UNCONVERTIBLE_FREQUENCY")
    unit = parts[1].lower() if len(parts) > 1 else ""
    if unit and unit != "mhz":
        return Quantity(None, "MHz", raw, "spectrometer_frequency", error=f"UNSUPPORTED_UNIT:{unit}")
    if not unit:
        return Quantity(value, "MHz", raw, "spectrometer_frequency", error="MISSING_UNIT")
    return Quantity(value, "MHz", raw, "spectrometer_frequency")


def parse_temperature(raw: str) -> Quantity:
    raw = (raw or "").strip()
    parts = raw.split()
    try:
        value = _number(parts[0])
    except (ValueError, IndexError, TypeError):
        return Quantity(None, "K", raw, "temperature", error="UNCONVERTIBLE_TEMPERATURE")
    unit = parts[1].upper() if len(parts) > 1 else ""
    if unit == "C":
        value = value + 273.15
        unit = "K"
    if unit and unit != "K":
        return Quantity(None, "K", raw, "temperature", error=f"UNSUPPORTED_UNIT:{unit}")
    if not unit:
        return Quantity(value, "K", raw, "temperature", error="MISSING_UNIT")
    return Quantity(value, "K", raw, "temperature")


def parse_float(raw: str, unit: str, datum: str, code: str) -> Quantity:
    raw = (raw or "").strip()
    try:
        return Quantity(_number(raw), unit, raw, datum)
    except (ValueError, TypeError):
        return Quantity(None, unit, raw, datum, error=code)
