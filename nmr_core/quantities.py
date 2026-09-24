"""Domain quantities.

Every domain value keeps its *raw* representation exactly as submitted,
plus an optional normalized SI-ish value computed only in the derived
layer. Values that cannot be converted stay in place with a diagnostic;
they are never dropped for the sake of plotting.
"""
from __future__ import annotations

import re
from typing import Any, Optional

KNOWN_UNITS = {
    "ppm": {"kind": "chemical_shift", "factor": 1.0},
    "hz": {"kind": "frequency", "factor": 1.0},
    "mhz": {"kind": "frequency", "factor": 1.0e6},
    "k": {"kind": "temperature", "factor": 1.0},
    "c": {"kind": "temperature", "factor": 1.0, "offset": 273.15},
    "au": {"kind": "intensity", "factor": 1.0},
    "count": {"kind": "integral", "factor": 1.0},
}

KNOWN_BASES = {"TMS", "DSS", "residual-solvent", "none"}

_NUM_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-z]+)?\s*(?:\(([^)]+)\))?\s*$")


def make_quantity(raw: Any, unit_hint: Optional[str] = None,
                  basis: Optional[str] = None) -> dict:
    """Build a quantity dict from a raw submission. Never raises."""
    q: dict[str, Any] = {
        "raw": raw if isinstance(raw, str) else repr(raw),
        "value": None,
        "unit": unit_hint,
        "basis": basis,
        "status": "unparsed",
        "diagnostic": None,
    }
    if isinstance(raw, (int, float)):
        q_value = float(raw)
        unit = unit_hint
    else:
        m = _NUM_RE.match(str(raw))
        if not m:
            q["diagnostic"] = f"cannot parse numeric value from {raw!r}; kept raw"
            return q
        q_value = float(m.group(1))
        unit = (m.group(2) or unit_hint)
        if m.group(3) and not basis:
            q["basis"] = m.group(3)
    if unit is None:
        q["value"] = q_value
        q["status"] = "ok"
        q["diagnostic"] = "no unit supplied; stored as bare number"
        return q
    unit_l = unit.lower()
    if unit_l not in KNOWN_UNITS:
        q["unit"] = unit_l
        q["diagnostic"] = f"unknown unit {unit!r} not convertible; value kept un-normalized"
        return q
    q["value"] = q_value
    q["unit"] = unit_l
    q["status"] = "ok"
    return q


def normalized(q: dict) -> Optional[float]:
    """Normalized numeric value, or None when not convertible."""
    return q.get("value")
