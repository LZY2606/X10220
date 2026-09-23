"""Quantities carry unit + coordinate/reference basis. Raw representation is
kept at import; normalization happens only in the derived layer. Values that
cannot be converted stay in place with a diagnostic."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Quantity:
    raw: str
    value: Optional[float]
    unit: str
    basis: Optional[str] = None
    ok: bool = True
    diagnostic: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "value": self.value,
            "unit": self.unit,
            "basis": self.basis,
            "ok": self.ok,
            "diagnostic": self.diagnostic,
        }


def make_quantity(raw: str, unit: str, basis: Optional[str] = None) -> Quantity:
    try:
        return Quantity(raw=raw, value=float(raw), unit=unit, basis=basis)
    except (TypeError, ValueError):
        return Quantity(raw=raw, value=None, unit=unit, basis=basis,
                        ok=False, diagnostic=f"not-a-number:{raw}")


def to_ppm(q: Quantity, frequency_mhz: Optional[float]) -> Quantity:
    """Normalize a shift quantity to ppm. Unconvertible values keep their raw
    form and gain a diagnostic instead of being dropped."""
    if not q.ok:
        return q
    if q.unit == "ppm":
        return q
    if q.unit == "Hz":
        if not frequency_mhz:
            return Quantity(q.raw, None, q.unit, q.basis, False,
                            "no-frequency-for-Hz-conversion")
        return Quantity(q.raw, q.value / frequency_mhz, "ppm", q.basis)
    return Quantity(q.raw, None, q.unit, q.basis, False,
                    f"cannot-convert-unit:{q.unit}->ppm")
