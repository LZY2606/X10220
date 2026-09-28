from __future__ import annotations

import json
from typing import Any

import numpy as np

from .ids import candidate_id, candidate_result_id, stable_hash
from .parser import parsed_records


ALGORITHM_VERSION = "nmr-assignment-1.0.0"


def by_logical(source: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not source:
        return {}
    return {record["logical_id"]: record for record in parsed_records(source)}


def list_by_logical(source: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not source:
        return []
    return parsed_records(source)


def split_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in str(raw).split(";") if item.strip()]


def nearest_peak(value: float | None, peaks: list[dict[str, Any]], tolerance: float) -> list[dict[str, Any]]:
    if value is None:
        return []
    return [
        peak
        for peak in peaks
        if peak.get("shift_ppm") is not None and abs(float(peak["shift_ppm"]) - value) <= tolerance
    ]


def peak_reference(peak: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": peak["record_id"],
        "logical_id": peak["logical_id"],
        "value_id": peak.get("shift_ppm__value_id"),
        "shift_ppm": peak.get("shift_ppm"),
        "raw_shift": peak.get("shift_ppm__raw"),
        "row": peak.get("row"),
    }


def edge_reference(edge: dict[str, Any], experiment: str) -> dict[str, Any]:
    h_field = "proton_shift_ppm__value_id" if experiment in {"HSQC", "HMBC"} else "from_peak_id__value_id"
    c_field = "carbon_shift_ppm__value_id" if experiment in {"HSQC", "HMBC"} else "to_peak_id__value_id"
    return {
        "record_id": edge["record_id"],
        "logical_id": edge["logical_id"],
        "proton_value_id": edge.get(h_field),
        "carbon_value_id": edge.get(c_field),
        "proton_shift_ppm": edge.get("proton_shift_ppm", edge.get("from_peak_id")),
        "carbon_shift_ppm": edge.get("carbon_shift_ppm", edge.get("to_peak_id")),
    }


def evidence_sets(edges_by_experiment: dict[str, list[dict[str, Any]]]) -> tuple[set[str], set[str], set[str]]:
    cosy, hsqc, hmbc = set(), set(), set()
    for edge in edges_by_experiment.get("COSY", []):
        for source in [edge.get("from_atom_hint"), edge.get("to_atom_hint")]:
            cosy.update(split_ids(source))
    for edge in edges_by_experiment.get("HSQC", []):
        hsqc.update(split_ids(edge.get("from_atom_hint")))
        hsqc.update(split_ids(edge.get("to_atom_hint")))
    for edge in edges_by_experiment.get("HMBC", []):
        hmbc.update(split_ids(edge.get("from_atom_hint")))
        hmbc.update(split_ids(edge.get("to_atom_hint")))
    return cosy, hsqc, hmbc


def load_domain(state: dict[str, Any]) -> dict[str, Any]:
    atoms = list_by_logical(state["sources"].get("structure"))
    proton_peaks = list_by_logical(state["sources"].get("1H"))
    carbon_peaks = list_by_logical(state["sources"].get("13C"))
    experiments = {
        "COSY": list_by_logical(state["sources"].get("COSY")),
        "HSQC": list_by_logical(state["sources"].get("HSQC")),
        "HMBC": list_by_logical(state["sources"].get("HMBC")),
    }
    groups: dict[str, list[str]] = {}
    for atom in atoms:
        group = atom.get("equivalence_group")
        if group:
            groups.setdefault(str(group), []).append(atom["logical_id"])
    return {
        "atoms": atoms,
        "proton_peaks": proton_peaks,
        "carbon_peaks": carbon_peaks,
        "experiments": experiments,
        "groups": groups,
    }
