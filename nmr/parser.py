"""Original-file preserving parser for NMR peak lists and molecular skeletons.

Design contract
---------------
* Raw representation (every token, its unit / coordinate basis and source line)
  is retained. Normalisation to numeric values happens only on a derived field.
* A value that cannot be converted stays in place (``value is None``) with a
  diagnostic; it is never dropped to make the picture look complete.
* Structural format errors raise :class:`FormatError` carrying all errors so
  the import layer can reject a submission atomically with precise messages.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ident import PARSER_VERSION, stable_id

MULTIPLICITIES = {"s", "d", "t", "q", "m", "dd", "ddd", "dt", "td", "brs", "brd"}
NUCLEUS_KINDS = {"1H", "13C", "H", "C"}


@dataclass
class FormatError(Exception):
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "; ".join(self.errors)


def measure(raw: str, unit: str) -> dict:
    try:
        return {"raw": raw, "value": float(raw), "unit": unit}
    except (TypeError, ValueError):
        return {"raw": raw, "value": None, "unit": unit}


def _lines(text: str):
    for lineno, raw_line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        yield lineno, line, raw_line


def _measure_or_diag(raw: str, unit: str, warnings: list[str], lineno: int, what: str) -> dict:
    m = measure(raw, unit)
    if m["value"] is None:
        warnings.append(f"line {lineno}: unconvertible {what} {raw!r} kept as raw text")
    return m


def parse_peaklist(name: str, text: str, source_key: str) -> dict:
    """Parse a 1D/2D peak-list file into a version-independent record."""
    errors: list[str] = []
    warnings: list[str] = []
    kind = None
    source_name = name
    conditions = {
        "frequency": None,
        "solvent": None,
        "temperature": None,
        "reference": None,
    }
    peaks: list[dict] = []
    edges_by_type: dict[str, list[dict]] = {"COSY": [], "HSQC": [], "HMBC": []}
    section: str | None = None
    seen_peak_ids: set[str] = set()

    for lineno, line, raw_line in _lines(text):
        if line.startswith("%source"):
            tokens = line.split()
            attrs = dict(tok.split("=", 1) for tok in tokens[1:] if "=" in tok)
            kind = attrs.get("type", kind)
            source_name = attrs.get("name", source_name)
            if kind not in NUCLEUS_KINDS:
                errors.append(f"line {lineno}: %source requires type in 1H/13C, got {kind!r}")
            section = None
            continue
        if line.startswith("%skeleton"):
            errors.append(f"line {lineno}: skeleton file submitted as peak list")
            continue
        if line.startswith("@"):
            parts = line.split(None, 2)
            key = parts[0][1:].lower()
            if key not in conditions:
                warnings.append(f"line {lineno}: unknown header @{key} ignored")
                continue
            if len(parts) < 2:
                errors.append(f"line {lineno}: @{key} needs a value")
                continue
            raw_value = parts[1] if len(parts) == 2 else f"{parts[1]} {parts[2]}"
            if key == "frequency":
                unit = "MHz"
                num_token = raw_value.split()[0]
                if len(raw_value.split()) > 1:
                    unit = raw_value.split()[1]
                m = _measure_or_diag(num_token, unit, warnings, lineno, "frequency")
                conditions["frequency"] = m
            elif key == "temperature":
                tokens = raw_value.split()
                m = _measure_or_diag(tokens[0], tokens[1] if len(tokens) > 1 else "K",
                                     warnings, lineno, "temperature")
                conditions["temperature"] = m
            else:
                conditions[key] = {"raw": raw_value, "value": raw_value, "unit": "text"}
            continue
        if line.startswith("%peaks"):
            section = "peaks"
            continue
        if line.startswith("%edges"):
            tokens = line.split()
            etype = tokens[1].upper() if len(tokens) > 1 else ""
            if etype not in edges_by_type:
                errors.append(f"line {lineno}: %edges must be COSY/HSQC/HMBC, got {etype!r}")
                section = None
            else:
                section = f"edges:{etype}"
            continue
        if line.startswith("%"):
            errors.append(f"line {lineno}: unknown section {line.split()[0]!r}")
            section = None
            continue

        tokens = line.split()
        if section == "peaks":
            if len(tokens) < 2:
                errors.append(f"line {lineno}: peak needs at least id and shift")
                continue
            pid, shift_raw = tokens[0], tokens[1]
            if pid in seen_peak_ids:
                errors.append(f"line {lineno}: duplicate peak id {pid!r}")
            seen_peak_ids.add(pid)
            intensity = _measure_or_diag(tokens[2], "counts", warnings, lineno, "intensity") \
                if len(tokens) > 2 else {"raw": None, "value": None, "unit": "counts"}
            if len(tokens) > 2 and tokens[2] in ("n/a", "NA", "N/A", "-"):
                intensity = {"raw": tokens[2], "value": None, "unit": "counts"}
                warnings.append(f"line {lineno}: missing intensity for {pid} retained")
            if len(tokens) > 3:
                int_raw = tokens[3]
                if int_raw.lower() in ("n/a", "na", "-"):
                    integral = {"raw": int_raw, "value": None, "unit": "nuclei"}
                    warnings.append(f"line {lineno}: missing integral for {pid} retained")
                else:
                    integral = _measure_or_diag(int_raw, "nuclei", warnings, lineno, "integral")
            else:
                integral = {"raw": None, "value": None, "unit": "nuclei"}
            mult_raw = tokens[4] if len(tokens) > 4 else "-"
            multiplicity = {"raw": mult_raw, "value": mult_raw if mult_raw in MULTIPLICITIES else None,
                            "unit": "code"}
            if mult_raw != "-" and mult_raw not in MULTIPLICITIES:
                warnings.append(f"line {lineno}: unknown multiplicity {mult_raw!r} kept raw")
            peaks.append({
                "id": pid,
                "shift": _measure_or_diag(shift_raw, "ppm", warnings, lineno, "shift"),
                "intensity": intensity,
                "integral": integral,
                "multiplicity": multiplicity,
                "note": " ".join(tokens[5:]) if len(tokens) > 5 else "",
                "source_line": lineno,
                "stable_id": stable_id("pk", source_key, lineno),
            })
            continue
        if section and section.startswith("edges:"):
            etype = section.split(":", 1)[1]
            if len(tokens) < 2:
                errors.append(f"line {lineno}: {etype} edge needs two coordinates")
                continue
            note = " ".join(tokens[2:]) if len(tokens) > 2 else ""
            coords = [
                _measure_or_diag(tokens[0], "ppm", warnings, lineno, f"{etype} coordinate 1"),
                _measure_or_diag(tokens[1], "ppm", warnings, lineno, f"{etype} coordinate 2"),
            ]
            edges_by_type[etype].append({
                "type": etype,
                "coordinates": coords,
                "note": note,
                "source_line": lineno,
                "stable_id": stable_id("ed", source_key, etype, lineno),
            })
            continue
        errors.append(f"line {lineno}: content outside of any section: {line[:40]!r}")

    if kind is None:
        errors.append("missing %source header with type=1H or type=13C")
    if not peaks and not any(edges_by_type.values()):
        errors.append("file contains neither peaks nor edges")
    if errors:
        raise FormatError(errors)

    return {
        "parser_version": PARSER_VERSION,
        "name": source_name,
        "nucleus": kind,
        "conditions": conditions,
        "peaks": peaks,
        "edges": edges_by_type,
        "warnings": warnings,
    }


def parse_skeleton(name: str, text: str, source_key: str) -> dict:
    """Parse a molecular skeleton: sites (nuclei), bonds and symmetry groups."""
    errors: list[str] = []
    warnings: list[str] = []
    compound = name
    sites: list[dict] = []
    bonds: list[tuple[str, str]] = []
    section: str | None = None
    site_ids: set[str] = set()

    for lineno, line, _raw in _lines(text):
        if line.startswith("%skeleton"):
            tokens = line.split()
            attrs = dict(tok.split("=", 1) for tok in tokens[1:] if "=" in tok)
            compound = attrs.get("compound", compound)
            continue
        if line.startswith("%sites"):
            section = "sites"
            continue
        if line.startswith("%bonds"):
            section = "bonds"
            continue
        if line.startswith("%"):
            errors.append(f"line {lineno}: unknown section {line.split()[0]!r}")
            section = None
            continue
        tokens = line.split()
        if section == "sites":
            if len(tokens) < 6:
                errors.append(
                    f"line {lineno}: site needs id kind element attached shift multiplicity [symmetry]")
                continue
            sid, kind, element, attached, shift_raw, mult = tokens[:6]
            symmetry = tokens[6] if len(tokens) > 6 and tokens[6] != "-" else None
            if kind not in ("H", "C"):
                errors.append(f"line {lineno}: site kind must be H or C, got {kind!r}")
            if sid in site_ids:
                errors.append(f"line {lineno}: duplicate site id {sid!r}")
            site_ids.add(sid)
            mult_value = mult if mult in MULTIPLICITIES else None
            if mult != "-" and mult not in MULTIPLICITIES:
                warnings.append(f"line {lineno}: unknown multiplicity {mult!r} kept raw")
            sites.append({
                "id": sid,
                "kind": kind,
                "element": element,
                "attached": None if attached == "-" else attached,
                "expected_shift": _measure_or_diag(shift_raw, "ppm", warnings, lineno,
                                                   "expected shift"),
                "multiplicity": {"raw": mult, "value": mult_value, "unit": "code"},
                "symmetry": symmetry,
                "source_line": lineno,
                "stable_id": stable_id("st", source_key, lineno),
            })
        elif section == "bonds":
            if len(tokens) < 2:
                errors.append(f"line {lineno}: bond needs two site ids")
                continue
            a, b = tokens[0], tokens[1]
            if a not in site_ids:
                errors.append(f"line {lineno}: bond references unknown site {a!r}")
            if b not in site_ids:
                errors.append(f"line {lineno}: bond references unknown site {b!r}")
            if a == b:
                errors.append(f"line {lineno}: self bond {a!r}")
            bonds.append((a, b))

    if not sites:
        errors.append("skeleton contains no %sites")
    attached_sites = {s["attached"] for s in sites if s["attached"]}
    for missing in attached_sites - site_ids:
        errors.append(f"attached reference to unknown site {missing!r}")
    proton_hosts = [s for s in sites if s["kind"] == "H" and s["attached"]]
    for proton in proton_hosts:
        host = next((s for s in sites if s["id"] == proton["attached"]), None)
        if host and host["kind"] != "C":
            errors.append(f"proton {proton['id']!r} attached to non-carbon {host['id']!r}")
    if errors:
        raise FormatError(errors)

    return {
        "parser_version": PARSER_VERSION,
        "compound": compound,
        "sites": sites,
        "bonds": sorted(set(bonds)),
        "warnings": warnings,
    }
