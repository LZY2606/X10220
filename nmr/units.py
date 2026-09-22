"""Quantities with units / reference frames.

All domain numbers keep their original textual representation; normalization
happens only in the derived layer. Values that cannot be converted stay in
place with a diagnostic instead of being dropped for plotting convenience.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# canonical unit normalization table
_UNIT_ALIASES = {
    "ppm": "ppm",
    "hz": "Hz",
    "khz": "kHz",
    "mhz": "MHz",
    "t": "T",
    "k": "K",
    "au": "a.u.",
    "a.u.": "a.u.",
}

_QTY_RE = re.compile(
    r"^\s*(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"\s*(?P<unit>[^\s0-9][^\s]*)?\s*$"
)
_INT_RE = re.compile(r"^\s*[+-]?\d+\s*$")
_RANGE_RE = re.compile(
    r"^\s*(?P<a>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:-|–|to)\s*"
    r"(?P<b>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?P<unit>[^\s]+)?\s*$"
)


@dataclass
class Quantity:
    """A measured/declared number preserving its source spelling."""

    raw: str
    value: Optional[float] = None
    unit: Optional[str] = None
    numeric: bool = False
    diagnostic: Optional[str] = None
    lo: Optional[float] = None
    hi: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "numeric": self.numeric,
            "diagnostic": self.diagnostic,
            "lo": self.lo,
            "hi": self.hi,
        }


def normalize_unit(unit: Optional[str]) -> Optional[str]:
    if unit is None:
        return None
    return _UNIT_ALIASES.get(unit.strip().lower(), unit.strip())


def parse_quantity(raw, expected_unit=None):
    """Parse ``<number><unit>``; non-convertible text is retained verbatim.

    A bare integer is accepted as an integral count (numeric, no unit).
    A numeric value with an unexpected/unknown unit is kept numeric but gets
    a diagnostic; non-numeric text is kept non-numeric with a diagnostic.
    """
    text = "" if raw is None else str(raw).strip()
    q = Quantity(raw=text)
    if text == "":
        q.diagnostic = "missing value"
        return q
    if _INT_RE.match(text):
        q.value = int(text)
        q.numeric = True
        return q
    rng = _RANGE_RE.match(text)
    if rng:
        a, b = float(rng.group("a")), float(rng.group("b"))
        q.lo, q.hi = min(a, b), max(a, b)
        q.value = (q.lo + q.hi) / 2.0
        q.numeric = True
        q.unit = normalize_unit(rng.group("unit"))
        if expected_unit and q.unit != expected_unit:
            q.diagnostic = "unit %r, expected %r; raw value retained" % (
                q.unit,
                expected_unit,
            )
        return q
    m = _QTY_RE.match(text)
    if m:
        q.value = float(m.group("value"))
        q.numeric = True
        q.unit = normalize_unit(m.group("unit"))
        if expected_unit and q.unit != expected_unit:
            q.diagnostic = "unit %r, expected %r; raw value retained" % (
                q.unit,
                expected_unit,
            )
        return q
    q.diagnostic = "non-numeric value kept verbatim: %r" % text
    return q


def canonical(q: Quantity) -> Optional[float]:
    """Derived-layer normalization. Returns the value in its canonical unit.

    Non-numeric/unconvertible quantities yield ``None`` (callers must keep
    showing the raw text with its diagnostic).
    """
    if not q.numeric or q.value is None:
        return None
    if q.unit in (None, "ppm", "Hz", "kHz", "MHz", "T", "K", "a.u."):
        return q.value
    return None
