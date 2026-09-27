"""Raw import layer.

Original textual representation is always preserved. Numeric parsing only
produces a derived value; unparseable values stay in place with a diagnostic.
"""
from __future__ import annotations

from .errors import FormatError

PEAK_HEADER = ["peak", "nucleus", "shift_ppm", "intensity", "integration", "multiplicity"]
CORR_HEADER = ["kind", "a", "b"]
CORR_KINDS = ("COSY", "HSQC", "HMBC")


def parse_num(raw: str) -> tuple[float | None, str | None]:
    text = (raw or "").strip()
    if text == "":
        return None, None
    try:
        return float(text), None
    except ValueError:
        return None, f"unparseable numeric value {raw!r}; kept raw"


def _data_lines(text: str):
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def parse_peaks(text: str) -> list[dict]:
    lines = _data_lines(text)
    if not lines:
        raise FormatError("peak list is empty")
    header = [h.strip() for h in lines[0].split(",")]
    if header != PEAK_HEADER:
        raise FormatError(
            f"peak list header must be {','.join(PEAK_HEADER)}, got {','.join(header)}"
        )
    peaks = []
    for line_no, line in enumerate(lines[1:], start=2):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(PEAK_HEADER):
            raise FormatError(
                f"line {line_no}: expected {len(PEAK_HEADER)} columns, got {len(parts)}"
            )
        label, nucleus, shift, intensity, integration, multiplicity = parts
        if not label:
            raise FormatError(f"line {line_no}: empty peak label")
        shift_v, shift_d = parse_num(shift)
        int_v, int_d = parse_num(intensity)
        integ_v, integ_d = parse_num(integration)
        diags = [d for d in (shift_d, int_d, integ_d) if d]
        peaks.append(
            {
                "label": label,
                "nucleus": nucleus,
                "shift_raw": shift,
                "shift": shift_v,
                "intensity_raw": intensity,
                "intensity": int_v,
                "integration_raw": integration,
                "integration": integ_v,
                "multiplicity": multiplicity,
                "diag": "; ".join(diags) if diags else None,
            }
        )
    return peaks


def parse_correlations(text: str) -> list[dict]:
    lines = _data_lines(text)
    if not lines:
        raise FormatError("correlation list is empty")
    header = [h.strip() for h in lines[0].split(",")]
    if header != CORR_HEADER:
        raise FormatError(
            f"correlation header must be {','.join(CORR_HEADER)}, got {','.join(header)}"
        )
    edges = []
    for line_no, line in enumerate(lines[1:], start=2):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(CORR_HEADER):
            raise FormatError(
                f"line {line_no}: expected {len(CORR_HEADER)} columns, got {len(parts)}"
            )
        kind, a_label, b_label = parts
        kind_up = kind.upper()
        if kind_up not in CORR_KINDS:
            raise FormatError(
                f"line {line_no}: correlation kind must be one of {','.join(CORR_KINDS)}, got {kind}"
            )
        if not a_label or not b_label:
            raise FormatError(f"line {line_no}: empty correlation endpoint")
        edges.append({"kind": kind_up, "a": a_label, "b": b_label})
    return edges
