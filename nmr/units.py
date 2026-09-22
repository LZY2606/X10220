"""Unit and coordinate-basis handling for domain quantities.

Raw representations are preserved verbatim on import; normalization to ppm
happens only in the derived layer.  Values that cannot be converted stay in
place and carry a diagnostic instead of being dropped for plotting.
"""
from __future__ import annotations


def normalize_shift(raw_value, raw_unit, channel, params):
    """Return (value_ppm, diagnostic).  Exactly one of the two is None."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None, "unparseable shift value %r" % (raw_value,)
    unit = (raw_unit or "").strip()
    if unit == "ppm":
        return value, None
    if unit == "Hz":
        key = "frequency_mhz_%s" % channel
        freq = params.get(key)
        if not freq:
            return None, "cannot convert Hz to ppm: %s missing" % key
        return value / float(freq), None
    return None, "unsupported shift unit %r; value kept raw at original position" % (raw_unit,)
