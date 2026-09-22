"""Domain quantities: every derived/normalised value keeps raw text, unit and basis.

Design rule from the requirements:
  * All domain quantities carry units or coordinate basis.
  * Import preserves the original representation; the derived layer normalises.
  * Values that cannot be converted stay in place with a diagnostic instead of
    being silently dropped to make a drawing work.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any

# Canonical unit for each measurable kind.
CANONICAL: Dict[str, str] = {
    "chemical_shift": "ppm",
    "frequency": "MHz",
    "coupling": "Hz",
    "intensity": "a.u.",
    "integral": "H-equiv",
    "temperature": "K",
    "angle": "deg",
    "time": "s",
}

_TO_MHZ = {"MHz": 1.0, "Hz": 1e-6, "kHz": 1e-3, "GHz": 1e3}
_TO_HZ = {"Hz": 1.0, "kHz": 1e3}


@dataclass
class Diagnostic:
    code: str
    message: str
    raw: Optional[str] = None
    severity: str = "error"  # error | warning

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Quantity:
    """A measured value with its original spelling, unit and coordinate basis."""

    raw: str
    value: Optional[float]
    unit: Optional[str]
    kind: str
    basis: Optional[str] = None  # e.g. "ppm@TMS", "ppm@residual-solvent(CDCl3)"
    canonical_value: Optional[float] = None
    canonical_unit: Optional[str] = None
    diagnostics: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)

    def to_json(self) -> Dict[str, Any]:
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "kind": self.kind,
            "basis": self.basis,
            "canonical_value": self.canonical_value,
            "canonical_unit": self.canonical_unit,
            "diagnostics": [d.to_json() if isinstance(d, Diagnostic) else d
                            for d in self.diagnostics],
        }


def _to_float(token: str):
    try:
        return float(token.strip())
    except ValueError:
        return None


def parse_quantity(raw, kind, basis=None, default_unit=None):
    """Parse "1.234 ppm"-style tokens. Never raises; attaches diagnostics.

    Unconvertible values keep ``value=None`` in their original position.
    """
    text = "" if raw is None else str(raw).strip()
    q = Quantity(raw=text, value=None, unit=None, kind=kind, basis=basis)
    parts = text.split()
    if not parts:
        q.diagnostics.append(Diagnostic("EMPTY_VALUE", "empty measurement", text))
        return q
    num = _to_float(parts[0])
    if num is None:
        q.diagnostics.append(
            Diagnostic("UNPARSEABLE_NUMBER", f"cannot parse number {parts[0]!r}", text)
        )
        return q
    q.value = num
    unit = parts[1].strip() if len(parts) > 1 else default_unit
    if len(parts) > 2:
        q.diagnostics.append(
            Diagnostic("EXTRA_TOKENS", "more than one unit token; kept first", text,
                       "warning")
        )
    q.unit = unit
    canon_unit = CANONICAL.get(kind)

    if kind == "chemical_shift":
        if unit in ("ppm", None):
            q.canonical_value = num
            q.canonical_unit = "ppm"
        else:
            q.diagnostics.append(
                Diagnostic("BAD_SHIFT_UNIT", f"chemical shift in {unit!r}, not ppm",
                           text, "warning")
            )
    elif kind == "frequency":
        if unit in _TO_MHZ:
            q.canonical_value = num * _TO_MHZ[unit]
            q.canonical_unit = "MHz"
        else:
            q.diagnostics.append(
                Diagnostic("BAD_FREQUENCY_UNIT", f"unknown frequency unit {unit!r}",
                           text, "warning")
            )
    elif kind == "coupling":
        if unit in _TO_HZ:
            q.canonical_value = num * _TO_HZ[unit]
            q.canonical_unit = "Hz"
        elif unit in ("ppm", None):
            q.canonical_value = num  # normalised later only if axis frequency known
            q.canonical_unit = "ppm"
        else:
            q.diagnostics.append(
                Diagnostic("BAD_J_UNIT", f"coupling unit {unit!r} not Hz/ppm", text,
                           "warning")
            )
    elif kind == "temperature":
        if unit in ("K",):
            q.canonical_value = num
        elif unit in ("C", "°C"):
            q.canonical_value = num + 273.15
        else:
            q.diagnostics.append(
                Diagnostic("BAD_TEMPERATURE_UNIT", f"temperature unit {unit!r}", text,
                           "warning")
            )
        q.canonical_unit = "K"
    else:
        # intensity / integral / angle / time: keep as-is, canonical unit marker
        if unit is None:
            q.unit = canon_unit
        q.canonical_value = num
        q.canonical_unit = q.unit

    if canon_unit and q.canonical_unit is None:
        q.diagnostics.append(
            Diagnostic("UNCONVERTED", "value left at original position, not converted",
                       text, "warning")
        )
    return q


def condition_fingerprint(meta: Dict[str, Any]) -> str:
    """Stable fingerprint of solvent/temperature/reference condition fields."""
    keys = ("solvent", "temperature_raw", "reference", "spectrometer_frequency_raw")
    subset = {k: meta.get(k) for k in keys}
    import json
    return json.dumps(subset, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
