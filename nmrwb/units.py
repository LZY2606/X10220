"""Quantity parsing: keep the original token, normalize only when a unit is known.

Design rule (case pack requirement): the raw representation is preserved at the
import layer; normalization happens only in the derived layer.  Values that
cannot be converted stay in place with a diagnostic instead of being dropped.
"""
from __future__ import annotations

from dataclasses import dataclass

HZ = {"hz": 1.0, "khz": 1.0e3, "mhz": 1.0e6}


@dataclass
class Quantity:
    raw: str
    value: float | None
    unit: str | None
    basis: str | None
    ok: bool
    message: str | None = None

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "basis": self.basis,
            "ok": self.ok,
            "message": self.message,
        }


def _number(token: str) -> float:
    return float(token)


def parse_quantity(token: str, default_unit: str, basis: str | None = None) -> Quantity:
    """Parse tokens like ``7.26ppm``, ``400.13 MHz``, ``298.15K``, ``25C``.

    A bare number is accepted with ``default_unit``.  Unknown units or bad
    numbers return ``ok=False`` carrying the original token.
    """
    raw = token
    t = token.strip()
    if not t:
        return Quantity(raw, None, None, basis, False, "empty value")
    # allow an internal space: split on the first whitespace if present,
    # e.g. "0.42 a.u." -> ("0.42", "a.u."); else peel a trailing unit suffix.
    if any(ch.isspace() for ch in t):
        head, _, tail = t.partition(next(ch for ch in t if ch.isspace()))
        num_part, unit_part = head.strip(), tail.strip().lower()
    else:
        i = len(t)
        while i > 0 and (t[i - 1].isalpha() or t[i - 1] in "%."):
            i -= 1
        num_part, unit_part = t[:i].strip(), t[i:].strip().lower()
    if not unit_part:
        unit_part = default_unit.lower()
    try:
        num = _number(num_part)
    except ValueError:
        return Quantity(raw, None, unit_part or None, basis, False,
                        f"not a number: {num_part!r}")
    if unit_part in ("ppm",):
        return Quantity(raw, num, "ppm", basis or "delta-scale", True)
    if unit_part in ("ppb",):
        return Quantity(raw, num * 1.0e-3, "ppm", basis or "delta-scale", True)
    if unit_part in HZ:
        return Quantity(raw, num * HZ[unit_part], "Hz", basis or "instrument-frequency", True)
    if unit_part in ("k",):
        return Quantity(raw, num, "K", basis or "temperature", True)
    if unit_part in ("c",):
        return Quantity(raw, num + 273.15, "K", basis or "temperature", True)
    normalized_unit = unit_part.replace(".", "").strip()
    if normalized_unit in ("", "none", "au", "count", "counts"):
        return Quantity(raw, num, default_unit, basis, True)
    return Quantity(raw, None, unit_part, basis, False,
                    f"unknown unit {unit_part!r}; value kept at source")


def parse_unitless(token: str, basis: str) -> Quantity:
    return parse_quantity(token, default_unit="count", basis=basis)
