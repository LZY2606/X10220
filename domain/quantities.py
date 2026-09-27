"""Physical quantities.

Every domain value keeps its original textual representation and unit/coordinate
reference. Normalisation to canonical units happens only on a derived layer;
unconvertible values stay in place with a diagnostic instead of being dropped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PPM_SPECTRA = {"1H", "13C", "19F", "31P", "15N"}


@dataclass(frozen=True)
class Quantity:
    """Value carrying unit/reference plus the original raw token."""

    value: float | None
    unit: str
    raw: str
    reference: str | None = None
    diagnostic: str | None = None

    def to_dict(self) -> dict:
        out = {"value": self.value, "unit": self.unit, "raw": self.raw}
        if self.reference is not None:
            out["reference"] = self.reference
        if self.diagnostic is not None:
            out["diagnostic"] = self.diagnostic
        return out


def quantity_from_token(token: object, allowed: tuple[str, ...]) -> Quantity:
    """Parse tokens like ``7.26 ppm @TMS`` or ``289.4 Hz``.

    ``allowed`` lists accepted unit strings. A token with an unknown unit is
    retained verbatim with a diagnostic (value None) so nothing is discarded.
    """

    raw = str(token)
    text = raw.strip()
    parts = text.split(None, 1)
    reference = None
    if len(parts) != 2:
        return Quantity(None, "?", raw, diagnostic="expected '<number> <unit>'")
    number, unit = parts
    if "@" in unit:
        unit, reference = (s.strip() for s in unit.split("@", 1))
    try:
        value = float(number)
    except ValueError:
        return Quantity(None, unit, raw, reference, "non-numeric value")
    if math.isnan(value) or math.isinf(value):
        return Quantity(None, unit, raw, reference, "non-finite value")
    if unit not in allowed:
        return Quantity(value, unit, raw, reference, f"unconvertible unit '{unit}'")
    return Quantity(value, unit, raw, reference)


def shift(token: object, nucleus: str) -> Quantity:
    allowed = ("ppm", "Hz")
    q = quantity_from_token(token, allowed)
    if q.unit == "Hz":
        return Quantity(q.value, "Hz", q.raw, q.reference,
                        "Hz shift retained; needs instrument frequency for ppm conversion")
    if q.value is not None and nucleus in PPM_SPECTRA:
        return Quantity(q.value, "ppm", q.raw, q.reference)
    return q


def ppm_value(q: Quantity, frequency_mhz: float | None) -> float | None:
    """Derived conversion. Hz can only be converted when frequency is known."""

    if q.value is None:
        return None
    if q.unit == "ppm":
        return q.value
    if q.unit == "Hz":
        return q.value / frequency_mhz if frequency_mhz else None
    return None


def hz_from_ppm(ppm: float, frequency_mhz: float) -> float:
    return ppm * frequency_mhz
