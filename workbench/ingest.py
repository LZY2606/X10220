"""Import layer.

Raw input is preserved verbatim.  Parsed records keep their original textual
representation (``raw``) alongside normalized values.  Values that cannot be
converted stay in place (normalized value is ``None``) and produce a diagnostic;
they are never dropped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

VALID_MULTIPLICITIES = {
    "s", "d", "t", "q", "dd", "dt", "td", "m",
    "singlet", "doublet", "triplet", "quartet", "sextet", "septet", "multiplet",
}
MULTIPLICITY_ALIASES = {
    "s": "singlet", "d": "doublet", "t": "triplet", "q": "quartet",
}
VALID_EXPERIMENTS = {"COSY", "HSQC", "HMBC"}
VALID_NUCLEI = {"1H", "13C"}


@dataclass
class Diagnostic:
    line: int
    code: str
    message: str
    severity: str = "error"
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "code": self.code, "message": self.message,
                "severity": self.severity, "raw": self.raw}


@dataclass
class ParsedSource:
    kind: str
    records: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "records": self.records, "metadata": self.metadata,
                "diagnostics": [d.to_dict() for d in self.diagnostics]}


def _parse_float(token: str, line: int, field_name: str, diags: list[Diagnostic]) -> float | None:
    token = token.strip()
    if token == "":
        diags.append(Diagnostic(line, "missing_value", f"missing numeric value for {field_name}", "error", token))
        return None
    try:
        return float(token)
    except ValueError:
        diags.append(Diagnostic(line, "unconvertible_value",
                                f"cannot convert {field_name!r} value {token!r}; kept at original position",
                                "error", token))
        return None


def _header_value(text: str, key: str) -> str | None:
    prefix = f"# {key}:"
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith(prefix.lower()):
            return s.split(":", 1)[1].strip()
    return None


def _split_fields(raw_line: str) -> list[str]:
    return [part.strip() for part in raw_line.split(";")]


def parse_peak_list(text: str) -> ParsedSource:
    nucleus = _header_value(text, "nucleus") or ""
    kind = "h1_peaks" if nucleus == "1H" else "c13_peaks" if nucleus == "13C" else "peaks"
    ps = ParsedSource(kind=kind, metadata={"nucleus": nucleus})
    if nucleus not in VALID_NUCLEI:
        ps.diagnostics.append(Diagnostic(0, "bad_nucleus",
                                         f"# nucleus header must be 1H or 13C, got {nucleus!r}", "error", nucleus))
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        fields = _split_fields(s)
        if kind == "h1_peaks":
            if len(fields) < 4:
                ps.diagnostics.append(Diagnostic(lineno, "bad_field_count",
                                                 "1H row needs shift; intensity; integral; multiplicity [; j]",
                                                 "error", s))
                continue
            rec: dict[str, Any] = {
                "line": lineno, "raw": s, "nucleus": "1H",
                "shift_ppm": _parse_float(fields[0], lineno, "shift_ppm", ps.diagnostics),
                "raw_shift_ppm": fields[0],
                "intensity_count": _parse_float(fields[1], lineno, "intensity_count", ps.diagnostics),
                "raw_intensity_count": fields[1],
                "integral_protons": _parse_float(fields[2], lineno, "integral_protons", ps.diagnostics),
                "raw_integral_protons": fields[2],
                "multiplicity": None, "raw_multiplicity": fields[3],
                "j_hz": None, "raw_j_hz": fields[4] if len(fields) > 4 else "",
            }
            mult = fields[3].strip().lower()
            if mult not in VALID_MULTIPLICITIES:
                ps.diagnostics.append(Diagnostic(lineno, "unknown_multiplicity",
                                                 f"multiplicity {fields[3]!r} not in controlled vocabulary", "warning", s))
            else:
                rec["multiplicity"] = MULTIPLICITY_ALIASES.get(mult, mult)
            if len(fields) > 4 and fields[4].strip():
                rec["j_hz"] = _parse_float(fields[4], lineno, "j_hz", ps.diagnostics)
        else:
            if len(fields) < 2:
                ps.diagnostics.append(Diagnostic(lineno, "bad_field_count",
                                                 "13C row needs shift; intensity", "error", s))
                continue
            rec = {
                "line": lineno, "raw": s, "nucleus": "13C",
                "shift_ppm": _parse_float(fields[0], lineno, "shift_ppm", ps.diagnostics),
                "raw_shift_ppm": fields[0],
                "intensity_count": _parse_float(fields[1], lineno, "intensity_count", ps.diagnostics),
                "raw_intensity_count": fields[1],
            }
        ps.records.append(rec)
    return ps


def parse_correlation_list(text: str) -> ParsedSource:
    exp = _header_value(text, "experiment") or ""
    ps = ParsedSource(kind=exp.lower() + "_edges" if exp in VALID_EXPERIMENTS else "edges",
                      metadata={"experiment": exp})
    if exp not in VALID_EXPERIMENTS:
        ps.diagnostics.append(Diagnostic(0, "bad_experiment",
                                         f"# experiment header must be one of {sorted(VALID_EXPERIMENTS)}, got {exp!r}",
                                         "error", exp))
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        fields = _split_fields(s)
        if len(fields) < 2:
            ps.diagnostics.append(Diagnostic(lineno, "bad_field_count",
                                             "edge row needs at least two ppm coordinates", "error", s))
            continue
        rec = {
            "line": lineno, "raw": s, "experiment": exp,
            "a_ppm": _parse_float(fields[0], lineno, "axis_a_ppm", ps.diagnostics),
            "raw_a_ppm": fields[0],
            "b_ppm": _parse_float(fields[1], lineno, "axis_b_ppm", ps.diagnostics),
            "raw_b_ppm": fields[1],
            "note": fields[2] if len(fields) > 2 else "",
        }
        ps.records.append(rec)
    return ps


def _strict_float(obj: dict[str, Any], key: str, diags: list[Diagnostic], required: bool = True) -> float | None:
    if key not in obj or obj[key] is None:
        if required:
            diags.append(Diagnostic(0, "missing_value", f"missing {key}", "error", ""))
        return None
    val = obj[key]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        diags.append(Diagnostic(0, "unconvertible_value",
                                f"{key} must be a number with explicit unit, got {val!r}", "error", str(val)))
        return None
    return float(val)


def parse_context(text: str) -> ParsedSource:
    ps = ParsedSource(kind="context")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        ps.diagnostics.append(Diagnostic(exc.lineno, "bad_json", f"invalid JSON: {exc.msg}", "error", ""))
        return ps
    if obj.get("schema") != "nmr-context/1":
        ps.diagnostics.append(Diagnostic(0, "bad_schema", "context must declare schema nmr-context/1", "error",
                                         str(obj.get("schema"))))
    required_str = ["sample_id", "solvent", "reference_standard"]
    for key in required_str:
        if not isinstance(obj.get(key), str) or not obj.get(key):
            ps.diagnostics.append(Diagnostic(0, "missing_value", f"{key} must be a non-empty string", "error",
                                             str(obj.get(key, ""))))
    meta = {k: v for k, v in obj.items()}
    meta["temperature_k"] = _strict_float(obj, "temperature_k", ps.diagnostics)
    meta["spectrometer_frequency_h1_mhz"] = _strict_float(obj, "spectrometer_frequency_h1_mhz", ps.diagnostics)
    meta["spectrometer_frequency_c13_mhz"] = _strict_float(obj, "spectrometer_frequency_c13_mhz", ps.diagnostics,
                                                           required=False)
    ps.metadata = meta
    return ps


def parse_params(text: str) -> ParsedSource:
    ps = ParsedSource(kind="params")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        ps.diagnostics.append(Diagnostic(exc.lineno, "bad_json", f"invalid JSON: {exc.msg}", "error", ""))
        return ps
    if obj.get("schema") != "nmr-assignment-params/1":
        ps.diagnostics.append(Diagnostic(0, "bad_schema",
                                         "params must declare schema nmr-assignment-params/1", "error",
                                         str(obj.get("schema"))))
    numeric_keys = [
        "shift_tolerance_h_ppm", "shift_tolerance_c_ppm", "edge_match_tolerance_ppm",
        "weight_shift", "weight_multiplicity", "weight_integral", "weight_network",
        "global_offset_detect_threshold_ppm", "reject_shift_multiples", "tie_epsilon_ppm",
    ]
    for key in numeric_keys:
        if key in obj:
            val = _strict_float(obj, key, ps.diagnostics)
            if val is not None:
                ps.metadata[key] = val
    weights = [ps.metadata.get(k) for k in ("weight_shift", "weight_multiplicity",
                                            "weight_integral", "weight_network")]
    if all(w is not None for w in weights):
        total = sum(weights)
        if abs(total - 1.0) > 1e-9:
            ps.diagnostics.append(Diagnostic(0, "bad_weights",
                                             f"weights must sum to 1.0, got {total:.6f}", "error", str(total)))
    ps.metadata["label"] = obj.get("label", "")
    return ps


def parse_skeleton(text: str) -> ParsedSource:
    ps = ParsedSource(kind="skeleton")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        ps.diagnostics.append(Diagnostic(exc.lineno, "bad_json", f"invalid JSON: {exc.msg}", "error", ""))
        return ps
    if obj.get("schema") != "nmr-skeleton/1":
        ps.diagnostics.append(Diagnostic(0, "bad_schema", "skeleton must declare schema nmr-skeleton/1",
                                         "error", str(obj.get("schema"))))
    atom_ids = set()
    for atom in obj.get("atoms", []):
        aid = atom.get("id")
        if not aid:
            ps.diagnostics.append(Diagnostic(0, "missing_atom_id", "every atom needs an id", "error", str(atom)))
            continue
        if aid in atom_ids:
            ps.diagnostics.append(Diagnostic(0, "duplicate_atom_id", f"duplicate atom id {aid}", "error", aid))
        atom_ids.add(aid)
        if atom.get("element") not in {"C", "H", "O", "N", "F", "P", "S"}:
            ps.diagnostics.append(Diagnostic(0, "bad_element", f"atom {aid} has unknown element", "warning",
                                             str(atom.get("element"))))
    for bond in obj.get("bonds", []):
        for end in ("from", "to"):
            if bond.get(end) not in atom_ids:
                ps.diagnostics.append(Diagnostic(0, "dangling_bond",
                                                 f"bond references unknown atom {bond.get(end)}", "error",
                                                 str(bond)))
    site_ids = set()
    for site in obj.get("proton_sites", []):
        sid = site.get("id")
        site_ids.add(sid)
        if site.get("attached_atom") not in atom_ids:
            ps.diagnostics.append(Diagnostic(0, "dangling_site",
                                             f"proton site {sid} attached to unknown atom {site.get('attached_atom')}",
                                             "error", str(site)))
        for key in ("expected_shift_ppm", "expected_integral"):
            _strict_float(site, key, ps.diagnostics)
    for corr in obj.get("expected_correlations", {}).get("COSY", []):
        if corr.get("site") not in site_ids or corr.get("other_site") not in site_ids:
            ps.diagnostics.append(Diagnostic(0, "dangling_correlation",
                                             f"COSY expected edge references unknown site {corr}", "error", str(corr)))
    if not ps.diagnostics:
        ps.metadata = obj
    else:
        ps.metadata = obj
    return ps


KIND_PARSERS = {
    "h1_peaks": parse_peak_list,
    "c13_peaks": parse_peak_list,
    "cosy_edges": parse_correlation_list,
    "hsqc_edges": parse_correlation_list,
    "hmbc_edges": parse_correlation_list,
    "context": parse_context,
    "params": parse_params,
    "skeleton": parse_skeleton,
}


def parse_source(kind: str, text: str) -> ParsedSource:
    if kind not in KIND_PARSERS:
        ps = ParsedSource(kind=kind)
        ps.diagnostics.append(Diagnostic(0, "unknown_kind", f"unknown source kind {kind!r}", "error", kind))
        return ps
    parsed = KIND_PARSERS[kind](text)
    if kind in ("h1_peaks", "c13_peaks") and not parsed.records and parsed.ok:
        parsed.diagnostics.append(Diagnostic(0, "empty_peak_list", "peak list contains no data rows",
                                             "warning", ""))
    return parsed
