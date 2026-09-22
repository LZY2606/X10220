from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Dict, List, Tuple


REQUIRED_DOCUMENTS = {"project", "molecule", "one_dimensional", "parameters"}
NUCLEI = ("1H", "13C")


def stable_hash(value: Any) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def file_id(raw: str) -> str:
    return "src_" + stable_hash(raw)[:16]


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    return float(value)


def raw_value(value: Any, unit: str, path: str, basis: str = "") -> Dict[str, Any]:
    result = {
        "stable_id": f"val:{path}",
        "raw": deepcopy(value),
        "unit": unit,
        "normalized": None,
        "basis": basis,
        "diagnostics": [],
    }
    try:
        number = _as_float(value)
        if number < 0:
            raise ValueError("value must be non-negative")
        result["normalized"] = number
    except Exception as exc:
        result["diagnostics"].append(
            {
                "code": "UNCONVERTIBLE_VALUE",
                "path": path,
                "message": f"{value!r} was retained but cannot be converted to {unit}: {exc}",
            }
        )
    return result


def parse_source_document(raw_text: str, declared_version: str = "v1") -> Dict[str, Any]:
    """Parse the bundled JSON NMR source while retaining every raw representation."""
    import json

    try:
        doc = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
    if not isinstance(doc, dict):
        raise ValueError("source root must be an object")
    missing = sorted(REQUIRED_DOCUMENTS - doc.keys())
    if missing:
        raise ValueError("missing required section(s): " + ", ".join(missing))

    diagnostics: List[Dict[str, Any]] = []
    parsed = {
        "schema_version": doc.get("schema_version", "1.0"),
        "file_version": doc["project"].get("file_version", declared_version),
        "project": deepcopy(doc["project"]),
        "parameters": deepcopy(doc["parameters"]),
        "molecule": parse_molecule(doc["molecule"], diagnostics),
        "documents": {},
        "diagnostics": diagnostics,
        "raw": doc,
    }
    for nucleus in NUCLEI:
        if nucleus not in doc["one_dimensional"]:
            raise ValueError(f"missing one_dimensional.{nucleus}")
        parsed["documents"][nucleus] = parse_oned(doc["one_dimensional"][nucleus], nucleus, diagnostics)
    parsed["documents"]["COSY"] = parse_cosy(doc.get("cosy"), parsed["molecule"], diagnostics)
    parsed["documents"]["HSQC"] = parse_hsqc(doc.get("hsqc"), parsed["molecule"], diagnostics)
    parsed["documents"]["HMBC"] = parse_hmbc(doc.get("hmbc"), parsed["molecule"], diagnostics)
    return parsed


def parse_molecule(section: Dict[str, Any], diagnostics: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(section, dict) or "atoms" not in section or "bonds" not in section:
        raise ValueError("molecule requires atoms and bonds arrays")
    atoms = {}
    for atom in section["atoms"]:
        if "id" not in atom or "element" not in atom:
            raise ValueError("every molecule atom requires id and element")
        atom_id = str(atom["id"])
        if atom_id in atoms:
            raise ValueError(f"duplicate atom id {atom_id}")
        item = deepcopy(atom)
        item["stable_id"] = f"atom:{atom_id}"
        atoms[atom_id] = item
    bonds = []
    for index, bond in enumerate(section["bonds"]):
        if len(bond) < 2 or bond[0] not in atoms or bond[1] not in atoms:
            raise ValueError(f"invalid bond at index {index}")
        bonds.append([str(bond[0]), str(bond[1]), str(bond[2]) if len(bond) > 2 else "single"])
    symmetry = [sorted(map(str, group)) for group in section.get("symmetry_groups", [])]
    for group in symmetry:
        for atom_id in group:
            if atom_id not in atoms:
                raise ValueError(f"symmetry group contains unknown atom {atom_id}")
            atoms[atom_id]["symmetry_group"] = ":".join(group)
    return {
        "id": section.get("id", "compound"),
        "name": section.get("name", "Unnamed compound"),
        "atoms": atoms,
        "atom_order": [str(a["id"]) for a in section["atoms"]],
        "bonds": bonds,
        "symmetry_groups": symmetry,
    }


def parse_oned(section: Dict[str, Any], nucleus: str, diagnostics: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(section, dict) or "peaks" not in section:
        raise ValueError(f"one_dimensional.{nucleus} requires a peaks array")
    metadata = deepcopy(section.get("metadata", {}))
    required_meta = ("solvent", "instrument_frequency_mhz", "temperature_k", "reference_standard")
    for key in required_meta:
        if key not in metadata:
            raise ValueError(f"one_dimensional.{nucleus}.metadata.{key} is required")
    peaks = []
    for index, peak in enumerate(section["peaks"]):
        if "id" not in peak or "shift_ppm" not in peak:
            raise ValueError(f"{nucleus} peak {index} requires id and shift_ppm")
        prefix = f"one_dimensional.{nucleus}.peaks[{index}]"
        item = {
            "stable_id": f"peak:{nucleus}:{peak['id']}",
            "id": str(peak["id"]),
            "nucleus": nucleus,
            "shift": raw_value(peak["shift_ppm"], "ppm", f"{prefix}.shift_ppm", "delta-scale"),
            "intensity": raw_value(peak.get("intensity"), "arbitrary intensity", f"{prefix}.intensity"),
            "integration": raw_value(peak.get("integration"), "proton count", f"{prefix}.integration")
            if nucleus == "1H"
            else None,
            "multiplicity": {"raw": peak.get("multiplicity"), "normalized": peak.get("multiplicity")},
            "overlap_group": peak.get("overlap_group"),
            "raw": deepcopy(peak),
            "diagnostics": [],
        }
        if item["shift"]["normalized"] is None:
            item["diagnostics"].append(item["shift"]["diagnostics"][0])
        if item["intensity"]["normalized"] is None:
            item["diagnostics"].append(item["intensity"]["diagnostics"][0])
        if item["integration"] and item["integration"]["normalized"] is None:
            item["diagnostics"].append(item["integration"]["diagnostics"][0])
        allowed = {"s", "d", "t", "q", "dd", "dt", "m", "br s", None}
        if item["multiplicity"]["raw"] not in allowed:
            item["multiplicity"]["diagnostic"] = "unknown multiplicity retained without normalization"
        peaks.append(item)
    return {"metadata": metadata, "peaks": peaks, "raw": deepcopy(section)}


def _edge(section: Any, exp: str, index: int, diagnostics: List[Dict[str, Any]]) -> Dict[str, Any]:
    if "id" not in section or "from" not in section or "to" not in section:
        raise ValueError(f"{exp} edge {index} requires id, from and to")
    prefix = f"{exp.lower()}[{index}]"
    coords = section.get("coordinates_ppm", {})
    return {
        "stable_id": f"edge:{exp}:{section['id']}",
        "id": str(section["id"]),
        "experiment": exp,
        "from": str(section["from"]),
        "to": str(section["to"]),
        "coordinates": {
            "raw": deepcopy(coords),
            "normalized": [
                raw_value(coords.get("f1"), "ppm", f"{prefix}.coordinates_ppm.f1"),
                raw_value(coords.get("f2"), "ppm", f"{prefix}.coordinates_ppm.f2"),
            ],
        },
        "intensity": raw_value(section.get("intensity"), "arbitrary intensity", f"{prefix}.intensity"),
        "raw": deepcopy(section),
    }


def parse_cosy(section: Any, molecule: Dict[str, Any], diagnostics: List[Dict[str, Any]]) -> Dict[str, Any]:
    section = section or {"edges": []}
    edges = [_edge(edge, "COSY", i, diagnostics) for i, edge in enumerate(section.get("edges", []))]
    return {"metadata": deepcopy(section.get("metadata", {})), "edges": edges, "raw": deepcopy(section)}


def parse_hsqc(section: Any, molecule: Dict[str, Any], diagnostics: List[Dict[str, Any]]) -> Dict[str, Any]:
    section = section or {"edges": []}
    edges = [_edge(edge, "HSQC", i, diagnostics) for i, edge in enumerate(section.get("edges", []))]
    return {"metadata": deepcopy(section.get("metadata", {})), "edges": edges, "raw": deepcopy(section)}


def parse_hmbc(section: Any, molecule: Dict[str, Any], diagnostics: List[Dict[str, Any]]) -> Dict[str, Any]:
    section = section or {"edges": []}
    edges = [_edge(edge, "HMBC", i, diagnostics) for i, edge in enumerate(section.get("edges", []))]
    return {"metadata": deepcopy(section.get("metadata", {})), "edges": edges, "raw": deepcopy(section)}


def source_metadata_keys(parsed: Dict[str, Any]) -> List[str]:
    keys = []
    for nucleus in NUCLEI:
        meta = parsed["documents"][nucleus]["metadata"]
        keys.extend([f"meta:{nucleus}:{key}:{meta[key]}" for key in ("solvent", "temperature_k", "reference_standard")])
    return keys
