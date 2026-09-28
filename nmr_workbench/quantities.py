from __future__ import annotations

import math
from typing import Any

from .ids import value_id


DOMAIN_UNITS: dict[tuple[str, str], dict[str, str]] = {
    ("structure", "x_angstrom"): {"unit": "Å", "coordinate_base": "molecular Cartesian frame"},
    ("structure", "y_angstrom"): {"unit": "Å", "coordinate_base": "molecular Cartesian frame"},
    ("structure", "z_angstrom"): {"unit": "Å", "coordinate_base": "molecular Cartesian frame"},
    ("structure", "expected_shift_ppm"): {"unit": "ppm", "coordinate_base": "nucleus-specific delta scale"},
    ("structure", "integral_proton"): {"unit": "1H count", "coordinate_base": "stoichiometric proton integral"},
    ("1H", "shift_ppm"): {"unit": "ppm", "coordinate_base": "1H delta scale"},
    ("1H", "integral_proton"): {"unit": "1H count", "coordinate_base": "relative proton integral"},
    ("1H", "intensity_counts"): {"unit": "counts", "coordinate_base": "instrument intensity axis"},
    ("13C", "shift_ppm"): {"unit": "ppm", "coordinate_base": "13C delta scale"},
    ("13C", "integral_carbon"): {"unit": "13C count", "coordinate_base": "assigned carbon environment count"},
    ("13C", "intensity_counts"): {"unit": "counts", "coordinate_base": "instrument intensity axis"},
    ("COSY", "from_peak_id"): {"unit": "ppm", "coordinate_base": "1H F2 delta scale"},
    ("COSY", "to_peak_id"): {"unit": "ppm", "coordinate_base": "1H F1 delta scale"},
    ("HSQC", "proton_shift_ppm"): {"unit": "ppm", "coordinate_base": "1H F2 delta scale"},
    ("HSQC", "carbon_shift_ppm"): {"unit": "ppm", "coordinate_base": "13C F1 delta scale"},
    ("HMBC", "proton_shift_ppm"): {"unit": "ppm", "coordinate_base": "1H F2 delta scale"},
    ("HMBC", "carbon_shift_ppm"): {"unit": "ppm", "coordinate_base": "13C F1 delta scale"},
}


NUMERIC_FIELDS = {
    "x_angstrom",
    "y_angstrom",
    "z_angstrom",
    "expected_shift_ppm",
    "integral_proton",
    "integral_carbon",
    "shift_ppm",
    "intensity_counts",
    "from_peak_id",
    "to_peak_id",
    "proton_shift_ppm",
    "carbon_shift_ppm",
    "temperature_kelvin",
    "proton_frequency_mhz",
    "carbon_frequency_mhz",
    "field_strength_tesla",
}


ACQUISITION_UNITS = {
    "temperature_kelvin": ("K", "sample temperature"),
    "proton_frequency_mhz": ("MHz", "1H instrument frequency"),
    "carbon_frequency_mhz": ("MHz", "13C instrument frequency"),
    "field_strength_tesla": ("T", "magnetic field"),
}


def make_value(
    source_id: str,
    logical_record_id: str,
    field: str,
    raw: Any,
    *,
    kind: str = "",
    numeric: bool | None = None,
) -> dict[str, Any]:
    raw_text = "" if raw is None else str(raw)
    is_numeric = field in NUMERIC_FIELDS if numeric is None else numeric
    parsed: Any = None
    valid = True
    diagnostic = None
    if raw is None or raw_text == "":
        parsed = None
    elif is_numeric:
        try:
            parsed = float(raw_text)
            if not math.isfinite(parsed):
                raise ValueError("non-finite number")
        except (TypeError, ValueError):
            valid = False
            diagnostic = f"unconvertible numeric value {raw_text!r}; retained at source"
    else:
        parsed = raw_text

    meta = DOMAIN_UNITS.get((kind, field), {})
    return {
        "value_id": value_id(source_id, logical_record_id, field),
        "field": field,
        "raw": raw_text,
        "parsed": parsed,
        "unit": meta.get("unit"),
        "coordinate_base": meta.get("coordinate_base"),
        "valid": valid,
        "diagnostic": diagnostic,
        "source_value": True,
    }


def make_acquisition_value(source_id: str, field: str, raw: Any) -> dict[str, Any]:
    value = make_value(source_id, "acquisition", field, raw)
    if field in ACQUISITION_UNITS:
        unit, base = ACQUISITION_UNITS[field]
        value["unit"] = unit
        value["coordinate_base"] = base
    elif field in {"solvent", "reference_standard", "sample_name", "operator", "notes"}:
        value["coordinate_base"] = "acquisition metadata"
    return value
