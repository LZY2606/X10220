"""Parsing of the small, copy-pastable NMR case formats.

The import layer is deliberately strict about *structure* (so that format
errors are reported with a file + line) while keeping every domain value in
its original representation.  Nothing parseable is dropped: values that
cannot be converted are retained with a diagnostic.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from .quantities import Quantity, parse_float, parse_frequency, parse_ppm, parse_temperature

PEAK_FAMILIES = ("1H", "13C")
EDGE_KINDS = ("COSY", "HSQC", "HMBC")


@dataclass
class Diagnostic:
    level: str
    code: str
    line: int
    message: str
    ref: str = ""

    def to_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "line": self.line,
                "message": self.message, "ref": self.ref}


@dataclass
class Peak:
    id: str
    isotope: str
    shift: Quantity
    intensity: Quantity
    integral: Quantity
    multiplicity: str
    multiplicity_raw: str
    assignments_hint: list = field(default_factory=list)
    overlap_group: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "isotope": self.isotope,
            "shift": self.shift.to_dict(), "intensity": self.intensity.to_dict(),
            "integral": self.integral.to_dict(),
            "multiplicity": self.multiplicity, "multiplicity_raw": self.multiplicity_raw,
            "assignments_hint": list(self.assignments_hint),
            "overlap_group": self.overlap_group,
        }


@dataclass
class Edge:
    id: str
    kind: str
    from_ref: str
    to_ref: str
    strength_raw: str
    strength: Quantity
    raw_line: int

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "from_ref": self.from_ref,
                "to_ref": self.to_ref, "strength_raw": self.strength_raw,
                "strength": self.strength.to_dict(), "raw_line": self.raw_line}


@dataclass
class Acquisition:
    family: str
    raw_header: str
    version: str = ""
    solvent: str = ""
    solvent_raw: str = ""
    reference_raw: str = ""
    reference: Optional[float] = None
    reference_error: Optional[str] = None
    frequency: Optional[Quantity] = None
    temperature: Optional[Quantity] = None

    def to_dict(self) -> dict:
        return {
            "family": self.family, "raw_header": self.raw_header, "version": self.version,
            "solvent": self.solvent, "solvent_raw": self.solvent_raw,
            "reference_raw": self.reference_raw, "reference": self.reference,
            "reference_error": self.reference_error,
            "frequency": self.frequency.to_dict() if self.frequency else None,
            "temperature": self.temperature.to_dict() if self.temperature else None,
        }


@dataclass
class ParsedPeakFile:
    kind: str = "peak"
    family: str = ""
    raw: str = ""
    sha256: str = ""
    acquisition: Optional[Acquisition] = None
    peaks: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    valid: bool = True

    def to_dict(self) -> dict:
        return {"kind": self.kind, "family": self.family, "raw": self.raw,
                "sha256": self.sha256, "acquisition": self.acquisition.to_dict() if self.acquisition else None,
                "peaks": [p.to_dict() for p in self.peaks],
                "edges": [e.to_dict() for e in self.edges],
                "diagnostics": [d.to_dict() for d in self.diagnostics], "valid": self.valid}


def raw_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _split_records(text: str):
    """Yield (record_kind, record_id, [(raw_line_no, fields_without_leading_kw)], raw_line_text)."""
    pending = None
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        kw = fields[0].upper()
        if kw == "ACQ":
            yield ("ACQ", "acq", [(idx, fields[1:])], stripped)
        elif kw in ("PEAK", "EDGE"):
            if pending is not None:
                yield pending
            pending = (kw, fields[1] if len(fields) > 1 else "",
                       [(idx, fields[2:])], stripped)
        elif kw == "END":
            if pending is not None:
                yield pending
                pending = None
            else:
                yield ("END", "", [(idx, fields[1:])], stripped)
        else:
            if pending is not None:
                pending[2].append((idx, fields))
            else:
                yield ("UNKNOWN", "", [(idx, fields)], stripped)
    if pending is not None:
        yield pending


def _parse_kv(token: str):
    if "=" not in token:
        return None
    key, value = token.split("=", 1)
    return key.strip().lower(), value.strip().strip('"')


def _kv_pairs(diag, line_no, fields):
    pairs = {}
    for token in fields:
        kv = _parse_kv(token)
        if kv is None:
            diag.append(Diagnostic("error", "BAD_KEYVALUE", line_no,
                                   f"expected key=value, got {token!r}"))
            continue
        key, value = kv
        if key in pairs:
            diag.append(Diagnostic("error", "DUPLICATE_KEY", line_no, f"duplicate key {key!r}"))
        pairs[key] = value
    return pairs


VALID_MULTIPLICITY = {
    "s": "s", "d": "d", "t": "t", "q": "q", "m": "m", "dd": "dd",
    "dq": "dq", "br s": "br s", "sext": "sext", "sept": "sept",
}


def parse_peak_file(text: str) -> ParsedPeakFile:
    parsed = ParsedPeakFile(raw=text, sha256=raw_hash(text))
    lines = text.splitlines()
    if not lines or not any(line.strip() for line in lines):
        parsed.diagnostics.append(Diagnostic("error", "EMPTY_FILE", 0, "file is empty"))
        parsed.valid = False
        return parsed
    header = next((line.strip() for line in lines if line.strip() and not line.strip().startswith("#")), "")
    if not header.upper().startswith("@NMRPEAK/"):
        parsed.diagnostics.append(Diagnostic("error", "BAD_HEADER", 1,
                                             "first non-comment line must be @NMRPEAK/1"))
        parsed.valid = False
    else:
        version_tag = header.split("/", 1)[1].strip()
        if version_tag != "1":
            parsed.diagnostics.append(Diagnostic("error", "UNSUPPORTED_FORMAT_VERSION", 1,
                                                 f"only @NMRPEAK/1 supported, got {version_tag!r}"))
            parsed.valid = False

    seen_peak_ids: dict[str, int] = {}
    seen_edge_ids: dict[str, int] = {}
    for kind, rec_id, grouped, raw_line in _split_records(text):
        first_line = grouped[0][0]
        if kind == "ACQ":
            if parsed.acquisition is not None:
                parsed.diagnostics.append(Diagnostic("error", "MULTIPLE_ACQ", first_line,
                                                     "only one ACQ record allowed"))
                parsed.valid = False
                continue
            pairs = _kv_pairs(parsed.diagnostics, first_line, grouped[0][1])
            family = pairs.get("family", "").strip()
            if family not in PEAK_FAMILIES:
                parsed.diagnostics.append(Diagnostic("error", "BAD_FAMILY", first_line,
                                                     f"family must be one of {PEAK_FAMILIES}", family))
                parsed.valid = False
                family = family or "?"
            acq = Acquisition(family=family, raw_header=raw_line, version=pairs.get("version", "").strip(),
                              solvent_raw=pairs.get("solvent", ""))
            acq.solvent = acq.solvent_raw
            freq_raw = pairs.get("frequency", "").strip()
            if freq_raw:
                acq.frequency = parse_frequency(freq_raw)
                if acq.frequency.error:
                    parsed.diagnostics.append(Diagnostic("error", acq.frequency.error, first_line,
                                                         f"frequency {freq_raw!r} retained as-is", "frequency"))
                    parsed.valid = False
            else:
                parsed.diagnostics.append(Diagnostic("error", "MISSING_FREQUENCY", first_line,
                                                     "ACQ lacks frequency"))
                parsed.valid = False
            temp_raw = pairs.get("temperature", "").strip()
            if temp_raw:
                acq.temperature = parse_temperature(temp_raw)
                if acq.temperature.error:
                    parsed.diagnostics.append(Diagnostic("error", acq.temperature.error, first_line,
                                                         f"temperature {temp_raw!r} retained as-is", "temperature"))
                    parsed.valid = False
            else:
                parsed.diagnostics.append(Diagnostic("error", "MISSING_TEMPERATURE", first_line,
                                                     "ACQ lacks temperature"))
                parsed.valid = False
            if "solvent" not in pairs or not pairs["solvent"]:
                parsed.diagnostics.append(Diagnostic("error", "MISSING_SOLVENT", first_line,
                                                     "ACQ lacks solvent"))
                parsed.valid = False
            if "version" not in pairs or not pairs["version"]:
                parsed.diagnostics.append(Diagnostic("error", "MISSING_VERSION", first_line,
                                                     "ACQ lacks version"))
                parsed.valid = False
            ref_raw = pairs.get("reference", "").strip()
            acq.reference_raw = ref_raw
            if ref_raw:
                ref_parts = ref_raw.split()
                nums = [part for part in ref_parts if _is_number(part)]
                if nums:
                    try:
                        acq.reference = float(nums[0])
                    except ValueError:
                        acq.reference_error = "UNCONVERTIBLE_REFERENCE"
                else:
                    acq.reference_error = "UNCONVERTIBLE_REFERENCE"
            parsed.acquisition = acq
            parsed.family = family
        elif kind == "PEAK":
            if not rec_id:
                parsed.diagnostics.append(Diagnostic("error", "MISSING_ID", first_line, "PEAK without id"))
                parsed.valid = False
                continue
            if rec_id in seen_peak_ids:
                parsed.diagnostics.append(Diagnostic("error", "DUPLICATE_PEAK_ID", first_line,
                                                     f"duplicate peak id {rec_id!r}", rec_id))
                parsed.valid = False
            seen_peak_ids[rec_id] = first_line
            parsed.peaks.append(_parse_peak(parsed, rec_id, grouped, parsed.family))
        elif kind == "EDGE":
            if not rec_id:
                parsed.diagnostics.append(Diagnostic("error", "MISSING_ID", first_line, "EDGE without id"))
                parsed.valid = False
                continue
            if rec_id in seen_edge_ids:
                parsed.diagnostics.append(Diagnostic("error", "DUPLICATE_EDGE_ID", first_line,
                                                     f"duplicate edge id {rec_id!r}", rec_id))
                parsed.valid = False
            seen_edge_ids[rec_id] = first_line
            parsed.edges.append(_parse_edge(parsed, rec_id, grouped))
        elif kind == "UNKNOWN":
            parsed.diagnostics.append(Diagnostic("error", "UNEXPECTED_TOKEN", first_line,
                                                 f"unrecognised line {raw_line!r}"))
            parsed.valid = False
        elif kind == "END":
            parsed.diagnostics.append(Diagnostic("error", "STRAY_END", first_line, "END outside a record"))
            parsed.valid = False

    if parsed.acquisition is None:
        parsed.diagnostics.append(Diagnostic("error", "MISSING_ACQ", 0, "file has no ACQ record"))
        parsed.valid = False
    return parsed


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def _record_kvs(parsed, grouped):
    merged: dict[str, tuple[str, int]] = {}
    for line_no, fields in grouped:
        if len(fields) == 2 and fields[0].isupper() is False and "=" in fields[0]:
            pass
        for token in fields:
            if token in {"END"}:
                continue
            kv = _parse_kv(token)
            if kv is None:
                key = fields[0].upper() if fields else ""
                parsed.diagnostics.append(Diagnostic("error", "BAD_KEYVALUE", line_no,
                                                     f"expected key=value inside record, got {token!r}"))
                continue
            key, value = kv
            if key in merged:
                parsed.diagnostics.append(Diagnostic("error", "DUPLICATE_KEY", line_no,
                                                     f"duplicate key {key!r}"))
            merged[key] = (value, line_no)
    return merged


def _parse_peak(parsed: ParsedPeakFile, peak_id: str, grouped, isotope: str) -> Peak:
    line0 = grouped[0][0]
    kvs = _record_kvs(parsed, grouped)
    shift_raw = kvs.get("shift", ("", line0))[0]
    shift = parse_ppm(shift_raw, isotope)
    if shift.error:
        parsed.diagnostics.append(Diagnostic("error", shift.error, kvs.get("shift", ("", line0))[1],
                                             f"shift {shift_raw!r} retained un-converted", peak_id))
        parsed.valid = False
    intensity_raw = kvs.get("intensity", ("", line0))[0]
    intensity = parse_float(intensity_raw, "a.u.", f"peak_intensity_{isotope}", "UNCONVERTIBLE_INTENSITY")
    if intensity.error:
        parsed.diagnostics.append(Diagnostic("error", intensity.error,
                                             kvs.get("intensity", ("", line0))[1],
                                             f"intensity {intensity_raw!r} retained un-converted", peak_id))
        parsed.valid = False
    integral_raw = kvs.get("integral", ("", line0))[0]
    integral = parse_float(integral_raw, "H", f"peak_integral_{isotope}", "UNCONVERTIBLE_INTEGRAL")
    if integral.error:
        parsed.diagnostics.append(Diagnostic("error", integral.error,
                                             kvs.get("integral", ("", line0))[1],
                                             f"integral {integral_raw!r} retained un-converted", peak_id))
        parsed.valid = False
    mult_raw = kvs.get("mult", ("", line0))[0]
    mult_norm = VALID_MULTIPLICITY.get(mult_raw.strip().lower(), mult_raw.strip().lower())
    if mult_raw.strip().lower() not in VALID_MULTIPLICITY:
        parsed.diagnostics.append(Diagnostic("warning", "UNKNOWN_MULTIPLICITY",
                                             kvs.get("mult", ("", line0))[1],
                                             f"multiplicity {mult_raw!r} not in standard set", peak_id))
    hints = [h.strip() for h in kvs.get("assignments", ("", line0))[0].split(",") if h.strip()]
    overlap = kvs.get("overlap", ("", None))[0] or None
    return Peak(id=peak_id, isotope=isotope, shift=shift, intensity=intensity, integral=integral,
                multiplicity=mult_norm, multiplicity_raw=mult_raw, assignments_hint=hints,
                overlap_group=overlap)


def _parse_edge(parsed: ParsedPeakFile, edge_id: str, grouped) -> Edge:
    line0 = grouped[0][0]
    kvs = _record_kvs(parsed, grouped)
    kind_raw = kvs.get("kind", ("", line0))[0].upper()
    if kind_raw not in EDGE_KINDS:
        parsed.diagnostics.append(Diagnostic("error", "BAD_EDGE_KIND", line0,
                                             f"edge kind must be one of {EDGE_KINDS}, got {kind_raw!r}",
                                             edge_id))
        parsed.valid = False
    from_ref = kvs.get("from", ("", line0))[0]
    to_ref = kvs.get("to", ("", line0))[0]
    if not from_ref:
        parsed.diagnostics.append(Diagnostic("error", "EDGE_MISSING_FROM", line0,
                                             "edge lacks from=<peak>", edge_id))
        parsed.valid = False
    if not to_ref:
        parsed.diagnostics.append(Diagnostic("error", "EDGE_MISSING_TO", line0,
                                             "edge lacks to=<peak>", edge_id))
        parsed.valid = False
    strength_raw = kvs.get("strength", ("", line0))[0]
    strength = parse_float(strength_raw, "a.u.", f"{kind_raw.lower()}_strength",
                           "UNCONVERTIBLE_STRENGTH")
    if strength_raw and strength.error:
        parsed.diagnostics.append(Diagnostic("error", strength.error,
                                             kvs.get("strength", ("", line0))[1],
                                             f"edge strength {strength_raw!r} retained un-converted", edge_id))
        parsed.valid = False
    return Edge(id=edge_id, kind=kind_raw, from_ref=from_ref, to_ref=to_ref,
                strength_raw=strength_raw, strength=strength, raw_line=line0)


@dataclass
class Site:
    id: str
    element: str
    predicted_shift: Quantity
    h_equiv: int
    multiplicity_hint: str
    symmetry_group: str
    symmetry_role: str
    neighbors: list = field(default_factory=list)
    attaches_to: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "element": self.element,
                "predicted_shift": self.predicted_shift.to_dict(),
                "h_equiv": self.h_equiv, "multiplicity_hint": self.multiplicity_hint,
                "symmetry_group": self.symmetry_group, "symmetry_role": self.symmetry_role,
                "neighbors": list(self.neighbors), "attaches_to": list(self.attaches_to)}


@dataclass
class Bond:
    a: str
    b: str
    order: str

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "order": self.order}


@dataclass
class ParsedSkeleton:
    kind: str = "skeleton"
    raw: str = ""
    sha256: str = ""
    compound: str = ""
    smiles: str = ""
    sites: list = field(default_factory=list)
    bonds: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    valid: bool = True

    def to_dict(self) -> dict:
        return {"kind": self.kind, "raw": self.raw, "sha256": self.sha256,
                "compound": self.compound, "smiles": self.smiles,
                "sites": [s.to_dict() for s in self.sites],
                "bonds": [b.to_dict() for b in self.bonds],
                "diagnostics": [d.to_dict() for d in self.diagnostics], "valid": self.valid}
