"""Quantities with explicit units / reference basis.

Source text is always preserved on ``raw``; normalization is derived.  A value
that cannot be converted is kept in place with a diagnostic instead of being
silently dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

# ppm is the NMR chemical-shift coordinate basis (relative dimensionless axis,
# but it is the documented reference basis and must be carried explicitly).
SHIFT_UNITS = {"ppm": 1.0}
FREQUENCY_UNITS = {"MHz": 1.0, "Hz": 1e-6, "kHz": 1e3}
TEMPERATURE_UNITS = {"K": (1.0, 0.0), "C": (1.0, 273.15)}

_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


@dataclass
class Quantity:
    value: float | None
    unit: str | None
    raw: str
    basis: str | None = None  # reference basis, e.g. "TMS" for shifts
    diagnostic: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_number(token: str) -> float:
    m = re.fullmatch(_NUM, token.strip())
    if not m:
        raise ValueError(f"not a number: {token!r}")
    return float(token)


def parse_shift(raw_text: str, reference: str | None = None) -> Quantity:
    """Parse a chemical shift token like ``4.98 ppm`` or ``4.98``."""
    text = raw_text.strip()
    m = re.fullmatch(rf"({_NUM})\s*([A-Za-z%]+)?", text)
    if not m:
        return Quantity(None, None, raw_text, reference,
                       diagnostic="unparseable chemical shift")
    value = float(m.group(1))
    unit = m.group(2) or "ppm"
    if unit not in SHIFT_UNITS:
        return Quantity(None, unit, raw_text, reference,
                       diagnostic=f"unknown shift unit {unit!r}")
    return Quantity(value * SHIFT_UNITS[unit], "ppm", raw_text, reference)


def parse_frequency(raw_text: str) -> Quantity:
    text = raw_text.strip()
    m = re.fullmatch(rf"({_NUM})\s*([A-Za-z]+)", text)
    if not m:
        return Quantity(None, None, raw_text,
                       diagnostic="unparseable instrument frequency")
    value, unit = float(m.group(1)), m.group(2)
    if unit not in FREQUENCY_UNITS:
        return Quantity(None, unit, raw_text,
                       diagnostic=f"unknown frequency unit {unit!r}")
    return Quantity(value * FREQUENCY_UNITS[unit], "MHz", raw_text)


def parse_temperature(raw_text: str) -> Quantity:
    text = raw_text.strip()
    m = re.fullmatch(rf"({_NUM})\s*([CFK])", text)
    if not m:
        return Quantity(None, None, raw_text,
                       diagnostic="unparseable temperature")
    value, unit = float(m.group(1)), m.group(2)
    factor, offset = TEMPERATURE_UNITS[unit]
    return Quantity(value * factor + offset, "K", raw_text)


def parse_integral(raw_text: str) -> Quantity:
    text = raw_text.strip()
    m = re.fullmatch(rf"({_NUM})\s*([A-Za-z%]+)?", text)
    if not m:
        return Quantity(None, None, raw_text,
                       diagnostic="unparseable integral")
    value = float(m.group(1))
    unit = m.group(2) or "H"  # proton-equivalent units
    if unit not in ("H", "relative", "%"):
        return Quantity(None, unit, raw_text,
                       diagnostic=f"unknown integral unit {unit!r}")
    return Quantity(value, unit, raw_text)


def parse_intensity(raw_text: str) -> Quantity:
    text = raw_text.strip()
    m = re.fullmatch(rf"({_NUM})\s*([A-Za-z%]+)?", text)
    if not m:
        return Quantity(None, None, raw_text,
                       diagnostic="unparseable intensity")
    return Quantity(float(m.group(1)), m.group(2) or "arbitrary", raw_text)


def quantity_to_dict(q: Quantity) -> dict:
    return q.to_dict()
