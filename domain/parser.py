"""Parsers for copy-paste friendly NMR peak-list and skeleton formats.

The parsers are intentionally strict about provenance and lenient about
science: every raw token is retained on the row, quantities keep their unit
basis, and bad rows produce diagnostics instead of aborting the document.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Any, Optional, Tuple

from .units import parse_quantity

PARSER_VERSION = "nmr-parser-1.0.0"

MULTIPLICITY_CODE = {
    "s": "singlet", "d": "doublet", "t": "triplet", "q": "quartet",
    "m": "multiplet", "br": "broad", "brs": "broad_singlet",
    "dd": "doublet_of_doublets", "dt": "doublet_of_triplets",
    "td": "triplet_of_doublets", "hept": "heptet",
}

_ISOTOPE_MAP = {"1H": "1H", "13C": "13C"}


def _stable(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]
    return "%s_%s" % (prefix, digest)


def _split_edge_refs(blob: str) -> List[str]:
    return [ref.strip() for ref in blob.split(",") if ref.strip()]


def parse_nmr(text: str, filename: str) -> Dict[str, Any]:
    """Parse a ``.nmr`` peak-list document.

    Format (``#`` comments, key: value headers)::

        experiment: 1H
        solvent: CDCl3
        frequency_mhz: 400.13 MHz
        temperature: 298 K
        reference: TMS
        --- peaks
        P1 shift=1.20 ppm intensity=0.92 integral=6H multiplicity=d J=7.0 Hz
        --- COSY
        P1 <-> P2
        --- HSQC
        P1 <-> C2
    """
    raw = text
    diagnostics: List[Dict[str, str]] = []
    headers: Dict[str, str] = {}
    peaks: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    section = "header"
    experiment = ""

    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("---"):
            name = stripped.strip("-").strip().upper()
            if name in ("PEAKS",):
                section = "peaks"
            elif name in ("COSY", "HSQC", "HMBC"):
                section = name
            else:
                diagnostics.append(_diag(lineno, stripped, "error",
                                         "unknown_section:%s" % name))
            continue
        if section == "header":
            key, _, value = stripped.partition(":")
            if not value:
                diagnostics.append(_diag(lineno, stripped, "error",
                                         "bad_header_line"))
                continue
            headers[key.strip().lower()] = value.strip()
            if key.strip().lower() == "experiment":
                experiment = value.strip()
            continue
        if section == "peaks":
            row = _parse_peak_line(stripped, lineno, experiment, diagnostics)
            if row is not None:
                peaks.append(row)
        else:
            row = _parse_edge_line(stripped, lineno, section, diagnostics)
            if row is not None:
                edges.append(row)

    experiment = headers.get("experiment", experiment)
    if experiment not in _ISOTOPE_MAP:
        diagnostics.append(_diag(0, experiment, "error",
                                 "missing_or_bad_experiment"))
    for required in ("solvent", "frequency_mhz", "reference"):
        if required not in headers:
            diagnostics.append(_diag(0, "", "error",
                                     "missing_header:%s" % required))

    conditions = {
        "solvent": headers.get("solvent", ""),
        "temperature": _header_quantity(headers, "temperature",
                                        "temperature", diagnostics),
        "reference": headers.get("reference", ""),
        "frequency_mhz": _header_quantity(headers, "frequency_mhz",
                                          "frequency", diagnostics),
        "experiment": _ISOTOPE_MAP.get(experiment, experiment),
    }
    document = {
        "kind": "nmr",
        "filename": filename,
        "parser_version": PARSER_VERSION,
        "raw": raw,
        "content_sha1": hashlib.sha1(raw.encode("utf-8")).hexdigest(),
        "headers": headers,
        "conditions": conditions,
        "peaks": peaks,
        "edges": edges,
        "diagnostics": diagnostics,
        "valid": not any(d["severity"] == "error" for d in diagnostics),
    }
    return document


def _header_quantity(headers: Dict[str, str], key: str, dimension: str,
                     diagnostics: List[Dict[str, str]]) -> Dict[str, Any]:
    raw = headers.get(key, "")
    parsed = parse_quantity(raw, dimension) if raw else parse_quantity("", dimension)
    if raw and not parsed.is_convertible():
        diagnostics.append({"line": 0, "raw": raw, "severity": "warning",
                            "code": parsed.diagnostic or "unconvertible"})
    return parsed.to_dict()


def _parse_peak_line(line: str, lineno: int, experiment: str,
                     diagnostics: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    tokens = line.split()
    label = tokens[0]
    if not re_like_label(label):
        diagnostics.append(_diag(lineno, line, "error", "bad_peak_label"))
        return None
    fields = _key_values(tokens[1:])
    if "shift" not in fields:
        diagnostics.append(_diag(lineno, line, "error", "peak_missing_shift"))
        return None
    shift = parse_quantity(fields["shift"], "chemical_shift")
    if not shift.is_convertible():
        diagnostics.append(_diag(lineno, line, "error",
                                 shift.diagnostic or "bad_shift"))
        return None
    intensity = parse_quantity(fields.get("intensity", ""), "intensity") \
        if "intensity" in fields else None
    integral = parse_quantity(fields.get("integral", ""), "integral") \
        if "integral" in fields else None
    multiplicity_raw = fields.get("multiplicity", "")
    multiplicity = {
        "raw": multiplicity_raw,
        "code": multiplicity_raw.lower(),
        "name": MULTIPLICITY_CODE.get(multiplicity_raw.lower(),
                                      multiplicity_raw or "unspecified"),
        "known": multiplicity_raw.lower() in MULTIPLICITY_CODE
                 if multiplicity_raw else True,
    }
    if multiplicity_raw and not multiplicity["known"]:
        diagnostics.append(_diag(lineno, line, "warning",
                                 "unknown_multiplicity:%s" % multiplicity_raw))
    j_coupling = None
    if "J" in fields:
        j_coupling = parse_quantity(fields["J"], "j_coupling")
        if not j_coupling.is_convertible():
            diagnostics.append(_diag(lineno, line, "warning",
                                     j_coupling.diagnostic or "bad_J"))
    notes = fields.get("notes", "")
    row = {
        "id": _stable("pk", "nmr", experiment, label),
        "label": label,
        "line": lineno,
        "nucleus": _ISOTOPE_MAP.get(experiment, experiment),
        "shift": shift.to_dict(),
        "intensity": intensity.to_dict() if intensity else None,
        "integral": integral.to_dict() if integral else None,
        "multiplicity": multiplicity,
        "j_coupling": j_coupling.to_dict() if j_coupling else None,
        "notes": notes,
        "raw": line,
    }
    return row


def re_like_label(token: str) -> bool:
    return bool(token) and token[0].isalnum()


def _parse_edge_line(line: str, lineno: int, kind: str,
                     diagnostics: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if "<->" not in line:
        diagnostics.append(_diag(lineno, line, "error", "edge_missing_arrow"))
        return None
    left, _, right = line.partition("<->")
    left_refs = _split_edge_refs(left)
    right_refs = _split_edge_refs(right)
    extra = ""
    if "#" in line:
        extra = line.split("#", 1)[1].strip()
    if not left_refs or not right_refs:
        diagnostics.append(_diag(lineno, line, "error", "edge_missing_endpoint"))
        return None
    row = {
        "id": _stable("edge", kind, line.strip()),
        "kind": kind,
        "line": lineno,
        "endpoints_a": left_refs,
        "endpoints_b": right_refs,
        "raw": line,
        "note": extra,
    }
    return row


def _key_values(tokens: List[str]) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    current: Optional[str] = None
    for token in tokens:
        if "=" in token:
            key, _, value = token.partition("=")
            current = key
            fields[key] = value
        elif current is not None:
            fields[current] = (fields[current] + " " + token).strip()
    return fields


def _diag(lineno: int, raw: str, severity: str, code: str) -> Dict[str, str]:
    return {"line": lineno, "raw": raw, "severity": severity, "code": code}


# ---------------------------------------------------------------------------
# Molecular skeleton (.molx)
# ---------------------------------------------------------------------------

MOLX_PARSER_VERSION = "molx-parser-1.0.0"


def parse_molx(text: str, filename: str) -> Dict[str, Any]:
    """Parse the domain skeleton format with atoms, spins and symmetry."""
    diagnostics: List[Dict[str, Any]] = []
    compound = filename
    atoms: List[Dict[str, Any]] = []
    bonds: List[Tuple[str, str, int]] = []
    spins: List[Dict[str, Any]] = []
    symmetry: List[Dict[str, Any]] = []
    section = "header"

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("---"):
            name = stripped.strip("-").strip().lower()
            if name not in ("atoms", "bonds", "spins", "symmetry"):
                diagnostics.append(_diag(lineno, stripped, "error",
                                         "unknown_section:%s" % name))
            section = name
            continue
        if section == "header":
            key, _, value = stripped.partition(":")
            if key.strip().lower() == "compound":
                compound = value.strip()
            else:
                diagnostics.append(_diag(lineno, stripped, "warning",
                                         "unknown_header:%s" % key.strip()))
        elif section == "atoms":
            parts = stripped.split()
            if len(parts) < 3:
                diagnostics.append(_diag(lineno, stripped, "error",
                                         "atom_needs_id_element_shift"))
                continue
            label, element, shift_raw = parts[0], parts[1], parts[2]
            shift = parse_quantity(shift_raw, "chemical_shift")
            pos = [float(v) for v in parts[3:5]] if len(parts) >= 5 else None
            if not shift.is_convertible():
                diagnostics.append(_diag(lineno, stripped, "error",
                                         shift.diagnostic or "bad_shift"))
                continue
            atoms.append({"id": label, "element": element,
                          "predicted_shift": shift.to_dict(),
                          "position": pos, "line": lineno, "raw": stripped})
        elif section == "bonds":
            parts = stripped.split()
            if len(parts) < 2:
                diagnostics.append(_diag(lineno, stripped, "error",
                                         "bond_needs_two_endpoints"))
                continue
            order = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
            bonds.append((parts[0], parts[1], order))
        elif section == "spins":
            parts = stripped.split()
            if len(parts) < 3:
                diagnostics.append(_diag(lineno, stripped, "error",
                                         "spin_needs_id_nucleus_atom"))
                continue
            spins.append({"id": parts[0], "nucleus": parts[1],
                          "atom": parts[2], "line": lineno, "raw": stripped})
        elif section == "symmetry":
            members = _split_edge_refs(stripped.replace("=", ","))
            if len(members) < 2:
                diagnostics.append(_diag(lineno, stripped, "error",
                                         "symmetry_needs_members"))
                continue
            group_id = _stable("sym", *members)
            symmetry.append({"id": group_id, "members": members,
                             "line": lineno, "raw": stripped})

    atom_ids = {atom["id"] for atom in atoms}
    spin_ids = {spin["id"] for spin in spins}
    for a, b, _order in bonds:
        if a not in atom_ids or b not in atom_ids:
            diagnostics.append(_diag(0, "%s-%s" % (a, b), "error",
                                     "bond_unknown_atom"))
    for spin in spins:
        if spin["atom"] not in atom_ids:
            diagnostics.append(_diag(spin["line"], spin["raw"], "error",
                                     "spin_unknown_atom"))
    for group in symmetry:
        for member in group["members"]:
            if member not in spin_ids and member not in atom_ids:
                diagnostics.append(_diag(group["line"], group["raw"], "error",
                                         "symmetry_unknown_member:%s" % member))

    document = {
        "kind": "skeleton",
        "filename": filename,
        "parser_version": MOLX_PARSER_VERSION,
        "raw": text,
        "content_sha1": hashlib.sha1(text.encode("utf-8")).hexdigest(),
        "compound": compound,
        "atoms": atoms,
        "bonds": [{"a": a, "b": b, "order": order} for a, b, order in bonds],
        "spins": spins,
        "symmetry": symmetry,
        "diagnostics": diagnostics,
        "valid": not any(d["severity"] == "error" for d in diagnostics),
    }
    return document


def parse_document(text: str, filename: str) -> Dict[str, Any]:
    if filename.endswith(".molx"):
        return parse_molx(text, filename)
    if filename.endswith(".nmr"):
        return parse_nmr(text, filename)
    raise ValueError("unsupported file type: %s" % filename)
