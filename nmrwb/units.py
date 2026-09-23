"""Domain quantities: raw representation is preserved at import; normalization
happens only in the derived layer. Values that cannot be converted stay in
place (raw kept, normalized=None) and produce a diagnostic instead of being
dropped."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

KNOWN_UNITS = {"ppm", "MHz", "Hz", "K", "C", "°C", "H", "Hz/2", ""}

_QTY_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([^\s\d]*)\s*$")


@dataclass
class Quantity:
    raw: object
    value: Optional[float]
    unit: Optional[str]
    ok: bool
    diagnostic: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "ok": self.ok,
            "diagnostic": self.diagnostic,
        }


def parse_quantity(raw, expected_unit: str = "") -> Quantity:
    """Parse a raw user/file value into a normalized float + unit.

    Numbers pass through with the expected unit. Strings like "4.02 ppm" are
    split into magnitude and unit. Anything else is returned with ok=False and
    a diagnostic; the raw value is never discarded.
    """
    if isinstance(raw, bool):
        return Quantity(raw, None, None, False, f"boolean is not a quantity: {raw!r}")
    if isinstance(raw, (int, float)):
        return Quantity(raw, float(raw), expected_unit, True)
    if not isinstance(raw, str):
        return Quantity(raw, None, None, False, f"unsupported quantity type: {type(raw).__name__}")
    m = _QTY_RE.match(raw)
    if not m:
        return Quantity(raw, None, None, False, f"unparseable quantity: {raw!r}")
    value = float(m.group(1))
    unit = m.group(2) or expected_unit
    if unit not in KNOWN_UNITS:
        return Quantity(raw, None, None, False, f"unknown unit {unit!r} in {raw!r}")
    if expected_unit and unit != expected_unit:
        conv = _convert(value, unit, expected_unit)
        if conv is None:
            return Quantity(raw, None, None, False,
                            f"cannot convert {unit!r} to {expected_unit!r} in {raw!r}")
        return Quantity(raw, conv, expected_unit, True)
    return Quantity(raw, value, unit, True)


def _convert(value: float, from_unit: str, to_unit: str) -> Optional[float]:
    if from_unit == to_unit:
        return value
    if from_unit in ("C", "°C") and to_unit == "K":
        return value + 273.15
    if from_unit == "K" and to_unit in ("C", "°C"):
        return value - 273.15
    if from_unit == "Hz" and to_unit == "MHz":
        return value / 1e6
    if from_unit == "MHz" and to_unit == "Hz":
        return value * 1e6
    return None


@dataclass
class NormalizedPeak:
    peak_id: str
    shift: Quantity
    intensity: Quantity
    integral: Quantity
    multiplicity: str
    raw: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.shift.ok


def normalize_peak(raw_peak: dict) -> tuple[NormalizedPeak, list[str]]:
    """Normalize one raw peak; returns (peak, diagnostics). Raw kept verbatim."""
    diags: list[str] = []
    shift = parse_quantity(raw_peak.get("shift"), "ppm")
    if not shift.ok:
        diags.append(f"peak {raw_peak.get('peak_id')}: {shift.diagnostic}")
    intensity = parse_quantity(raw_peak.get("intensity", 0), "")
    if not intensity.ok:
        diags.append(f"peak {raw_peak.get('peak_id')}: {intensity.diagnostic}")
    integral = parse_quantity(raw_peak.get("integral", 0), "H")
    if not integral.ok:
        diags.append(f"peak {raw_peak.get('peak_id')}: {integral.diagnostic}")
    mult = raw_peak.get("multiplicity", "unknown")
    peak = NormalizedPeak(
        peak_id=str(raw_peak.get("peak_id")),
        shift=shift, intensity=intensity, integral=integral,
        multiplicity=str(mult), raw=dict(raw_peak),
    )
    return peak, diags
