"""Domain quantities with units and coordinate references.

Every domain value keeps its *raw representation* exactly as written in the
source file.  Normalisation to a floating point number is performed only in the
derived layer.  Values that cannot be converted stay in place and carry a
diagnostic instead of being dropped to make a drawing render.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

# A chemical shift is a coordinate on an axis whose zero is fixed by a
# reference standard (TMS, DSS, ...).  The coordinate is meaningless without
# that basis, hence the ``basis`` field is mandatory on shifts.
SHIFT_UNITS = {"ppm"}
FREQUENCY_UNITS = {"mhz"}
TEMPERATURE_UNITS = {"k", "c", "°c"}
INTENSITY_UNITS = {"a.u.", "rel", "%"}
COORDINATE_BASES = {"TMS", "DSS", "TSP"}


class UnitError(ValueError):
    """Raised when a raw value cannot be converted to the expected unit."""


@dataclass(frozen=True)
class Quantity:
    """An immutable domain value.

    Attributes
    ----------
    raw:
        Original token from the source file, never rewritten.
    value:
        Normalised numeric value, or ``None`` when conversion failed.
    unit:
        Normalised unit string.
    basis:
        Coordinate reference (e.g. ``"TMS"``) for shift axes.
    diagnostic:
        Machine readable explanation when ``value is None``.
    """

    raw: str
    value: Optional[float]
    unit: str
    basis: Optional[str] = None
    diagnostic: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "basis": self.basis,
            "diagnostic": self.diagnostic,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Quantity":
        return cls(
            raw=data["raw"],
            value=data["value"],
            unit=data["unit"],
            basis=data.get("basis"),
            diagnostic=data.get("diagnostic"),
        )


def _normalise_unit(unit: str) -> str:
    return unit.strip().replace(" ", "").lower()


def _to_kelvin(number: float, unit: str) -> float:
    if unit == "k":
        return number
    return number + 273.15


def parse_quantity(raw: Any, *, allowed: set[str], basis: Optional[str] = None) -> Quantity:
    """Parse ``"<number> <unit>"`` while preserving the original token."""
    token = str(raw).strip()
    match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*([^\s\d][^\s]*)?\s*", token)
    if not match:
        return Quantity(raw=token, value=None, unit="", basis=basis,
                        diagnostic="not-a-number")
    number_text, unit_text = match.groups()
    if unit_text is None:
        return Quantity(raw=token, value=None, unit="", basis=basis,
                        diagnostic="missing-unit")
    unit = _normalise_unit(unit_text)
    if unit not in allowed:
        return Quantity(raw=token, value=None, unit=unit, basis=basis,
                        diagnostic=f"unsupported-unit:{unit}")
    number = float(number_text)
    if unit in TEMPERATURE_UNITS:
        number = _to_kelvin(number, unit)
        unit = "K"
        return Quantity(raw=token, value=number, unit=unit, basis=basis)
    if unit == "mhz":
        unit = "MHz"
    elif unit == "ppm":
        unit = "ppm"
        if basis is not None and basis not in COORDINATE_BASES:
            return Quantity(raw=token, value=None, unit=unit, basis=basis,
                            diagnostic=f"unknown-reference:{basis}")
    return Quantity(raw=token, value=number, unit=unit, basis=basis)


def parse_shift(raw: Any, *, basis: str) -> Quantity:
    return parse_quantity(raw, allowed=SHIFT_UNITS, basis=basis)


def parse_intensity(raw: Any) -> Quantity:
    return parse_quantity(raw, allowed=INTENSITY_UNITS)


def parse_frequency(raw: Any) -> Quantity:
    return parse_quantity(raw, allowed=FREQUENCY_UNITS)


def parse_temperature(raw: Any) -> Quantity:
    return parse_quantity(raw, allowed=TEMPERATURE_UNITS)


def parse_integration(raw: Any) -> Quantity:
    """Integration is a dimensionless proton count; it carries no unit."""
    token = str(raw).strip()
    match = re.fullmatch(r"[+-]?\d+(?:\.\d+)?\s*[Hh]?", token)
    if not match:
        return Quantity(raw=token, value=None, unit="H",
                        diagnostic="integration-not-numeric")
    number = float(re.match(r"[+-]?\d+(?:\.\d+)?", token).group(0))
    if number < 0:
        return Quantity(raw=token, value=None, unit="H",
                        diagnostic="negative-integration")
    return Quantity(raw=token, value=number, unit="H")


def parse_reference(raw: Any) -> str:
    """Reference standard token, validated but otherwise preserved verbatim."""
    token = str(raw).strip()
    if token not in COORDINATE_BASES:
        raise UnitError(f"unknown-reference:{token}")
    return token
