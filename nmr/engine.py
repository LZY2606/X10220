from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import numpy as np

from . import ALGORITHM_VERSION
from .model import Atom, Edge, Peak

DEFAULT_PARAMS: dict[str, Any] = {
    "tolerance_ppm": {"1H": 0.20, "13C": 2.0},
    "reference_offset_ppm": {"1H": 0.0, "13C": 0.0},
    "edge_match_ppm": {"1H": 0.03, "13C": 0.12},
    "missing_edge_penalty": 3.0,
}


def parameter_fingerprint(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def nucleus_parameter_fingerprint(params: dict[str, Any], nucleus: str) -> str:
    relevant = {
        "tolerance_ppm": params["tolerance_ppm"].get(nucleus),
        "reference_offset_ppm": params["reference_offset_ppm"].get(nucleus),
        "edge_match_ppm": params.get("edge_match_ppm", {}),
        "missing_edge_penalty": params.get("missing_edge_penalty"),
    }
    return json.dumps(relevant, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def generate_candidates(model: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    molecule = model.get("molecule")
    if molecule is None:
        return {"algorithm_version": ALGORITHM_VERSION, "parameter_fingerprint": parameter_fingerprint(params), "candidates": [], "peak_summaries": [], "global_bias": {}, "unknowns": []}
    atoms: dict[str, Atom] = molecule["atoms"]
    peaks: list[Peak] = model["peaks"]
    edges: list[Edge] = model["edges"]
    candidates: list[dict[str, Any]] = []
    valid_pairs_by_peak: dict[str, list[dict[str, Any]]] = defaultdict(list)
    residues_by_nucleus: dict[str, list[float]] = defaultdict(list)
    for peak in peaks:
        for atom in atoms.values():
            if atom.nucleus != peak.nucleus or atom.shift is None:
                continue
            offset = float(params["reference_offset_ppm"].get(peak.nucleus, 0.0))
            corrected = peak.shift + offset
            residual = corrected - atom.shift
            tolerance = float(params["tolerance_ppm"].get(peak.nucleus, 0.2))
            in_tolerance = abs(residual) <= tolerance
            candidate = {
                "candidate_id": f"cand:{peak.peak_id}!{atom.atom_id}",
                "stable_key": f"{peak.nucleus}:{peak.source_doc_id}:{_local(peak.peak_id)}!{atom.atom_id}",
                "peak_id": peak.peak_id,
                "atom_id": atom.atom_id,
                "nucleus": peak.nucleus,
                "observed_shift_ppm": peak.shift,
                "reference_offset_ppm": offset,
                "corrected_shift_ppm": corrected,
                "predicted_shift_ppm": atom.shift,
                "shift_error_ppm": round(residual, 6),
                "chemical_score": round(abs(residual) * 10.0, 6),
                "edge_score": 0.0,
                "edge_evidence": [],
                "missing_expected_edges": [],
                "rejection_reasons": [] if in_tolerance else [f"shift_error {abs(residual):.6f} ppm exceeds tolerance {tolerance:.6f} ppm"],
                "status": "legal" if in_tolerance else "rejected",
                "direct_sources": sorted({peak.source_doc_id, molecule["source_doc_id"]}),
            }
            candidates.append(candidate)
            if in_tolerance:
                valid_pairs_by_peak[peak.peak_id].append(candidate)
                residues_by_nucleus[peak.nucleus].append(residual)
    pair_index = {(item["peak_id"], item["atom_id"]): item for items in valid_pairs_by_peak.values() for item in items}
    atom_by_id = {atom.atom_id: atom for atom in atoms.values()}
    peak_index = {(peak.nucleus, peak.shift): peak for peak in peaks}
    adjacency = _adjacency(molecule["bonds"])
    for edge in edges:
        matched = _match_edge(edge, peaks, valid_pairs_by_peak, params)
        for peak_a, atom_a, peak_b, atom_b in matched:
            kind = _edge_kind(edge.experiment, atom_a, atom_b, adjacency)
            if kind is None:
                continue
            for candidate_atom, candidate_peak, other_atom in ((atom_a, peak_a, atom_b), (atom_b, peak_b, atom_a)):
                candidate = pair_index.get((candidate_peak.peak_id, candidate_atom.atom_id))
                if candidate is None:
                    continue
                evidence = {
                    "experiment": edge.experiment,
                    "edge_id": edge.edge_id,
                    "other_atom_id": other_atom.atom_id,
                    "observed_from_ppm": edge.from_shift,
                    "observed_to_ppm": edge.to_shift,
                    "relationship": kind,
                    "weight": 12.0 if kind == edge.experiment else 4.0,
                }
                candidate["edge_evidence"].append(evidence)
                candidate["edge_score"] -= evidence["weight"]
    for candidate in list(pair_index.values()):
        atom = atom_by_id[candidate["atom_id"]]
        nucleus = candidate["nucleus"]
        if nucleus != "1H":
            continue
        for expected in molecule["expected_edges"]:
            neighbor = None
            if expected.from_atom == atom.atom_id:
                neighbor = expected.to_atom
            elif expected.to_atom == atom.atom_id:
                neighbor = expected.from_atom
            if neighbor is None:
                continue
            experiments = {item["experiment"] for item in candidate["edge_evidence"]}
            if "COSY" not in experiments:
                candidate["missing_expected_edges"].append({"experiment": "COSY", "expected_partner_atom_id": neighbor, "interpretation": "evidence_missing_not_rejection"})
                candidate["edge_score"] += float(params["missing_edge_penalty"])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for peak_id, items in valid_pairs_by_peak.items():
        for item in items:
            grouped[peak_id].append(item)
    peak_summaries: list[dict[str, Any]] = []
    for peak_id, items in grouped.items():
        ordered = sorted(items, key=lambda item: (round(item["chemical_score"] + item["edge_score"], 6), item["atom_id"]))
        best_score = ordered[0]["chemical_score"] + ordered[0]["edge_score"] if ordered else None
        ranks: dict[str, int] = {}
        for item in ordered:
            total = round(item["chemical_score"] + item["edge_score"], 6)
            rank = 1 if best_score is not None and total == round(best_score, 6) else 0
            item["total_score"] = round(total, 6)
            item["rank"] = rank
            item["tie_group_size"] = sum(1 for other in ordered if round(other["chemical_score"] + other["edge_score"], 6) == total)
            ranks[item["candidate_id"]] = rank
        peak = next(peak for peak in peaks if peak.peak_id == peak_id)
        peak_summaries.append({"peak_id": peak_id, "nucleus": peak.nucleus, "legal_candidate_count": len(ordered), "rank_1_candidate_ids": [item["candidate_id"] for item in ordered if item["rank"] == 1]})
    for candidate in candidates:
        candidate["edge_score"] = round(float(candidate["edge_score"]), 6)
        candidate["total_score"] = round(candidate["chemical_score"] + candidate["edge_score"], 6)
        if candidate["status"] == "legal" and candidate["candidate_id"] not in ranks:
            candidate["rank"] = 0
            candidate["tie_group_size"] = 0
    unknowns = _unknowns(peaks, atoms, valid_pairs_by_peak)
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "parameter_fingerprint": parameter_fingerprint(params),
        "candidates": sorted(candidates, key=lambda item: (item["peak_id"], item["total_score"], item["atom_id"])),
        "peak_summaries": sorted(peak_summaries, key=lambda item: item["peak_id"]),
        "global_bias": {nucleus: {"median_legal_residual_ppm": round(float(np.median(np.asarray(values, dtype=float))), 6), "sample_count": len(values)} for nucleus, values in sorted(residues_by_nucleus.items())},
        "unknowns": unknowns,
    }


def _local(peak_id: str) -> str:
    return peak_id.split(":", 2)[2]


def _adjacency(bonds: list[tuple[str, str]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for left, right in bonds:
        result[left].add(right)
        result[right].add(left)
    return result


def _match_edge(edge: Edge, peaks: list[Peak], legal: dict[str, list[dict[str, Any]]], params: dict[str, Any]) -> set[tuple[Peak, Atom, Peak, Atom]]:
    peak_a = _nearest_peak(edge.from_shift, edge.from_unit, peaks, float(params["edge_match_ppm"].get(edge.from_unit and _infer_nucleus(edge, "from"), 0.03)))
    peak_b = _nearest_peak(edge.to_shift, edge.to_unit, peaks, float(params["edge_match_ppm"].get(edge.to_unit and _infer_nucleus(edge, "to"), 0.03)))
    if peak_a is None or peak_b is None:
        return set()
    result = set()
    for cand_a in legal.get(peak_a.peak_id, []):
        for cand_b in legal.get(peak_b.peak_id, []):
            result.add((peak_a, _AtomRef(cand_a["atom_id"]), peak_b, _AtomRef(cand_b["atom_id"])))
    return result


class _AtomRef:
    def __init__(self, atom_id: str):
        self.atom_id = atom_id


def _nearest_peak(shift: float, unit: str, peaks: list[Peak], tolerance: float) -> Peak | None:
    eligible = [peak for peak in peaks if peak.shift_unit == unit and abs(peak.shift - shift) <= tolerance]
    if not eligible:
        return None
    return sorted(eligible, key=lambda peak: (abs(peak.shift - shift), peak.peak_id))[0]


def _infer_nucleus(edge: Edge, side: str) -> str:
    return "1H" if (side == "from" and edge.experiment in {"COSY", "HSQC"}) or (side == "to" and edge.experiment == "HSQC") else "13C"


def _edge_kind(experiment: str, atom_a: Any, atom_b: Any, adjacency: dict[str, set[str]]) -> str | None:
    left, right = atom_a.atom_id, atom_b.atom_id
    distance = _distance(left, right, adjacency)
    if distance == 1:
        return "HSQC" if experiment == "HSQC" else ("COSY" if experiment == "COSY" else None)
    if experiment == "HMBC" and distance in {2, 3}:
        return "HMBC"
    if experiment == "HMBC" and distance == 1:
        return None
    return None


def _distance(source: str, target: str, adjacency: dict[str, set[str]]) -> int:
    if source == target:
        return 0
    queue = [(source, 0)]
    seen = {source}
    while queue:
        node, depth = queue.pop(0)
        for neighbor in adjacency.get(node, set()):
            if neighbor == target:
                return depth + 1
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, depth + 1))
    return 99


def _unknowns(peaks: list[Peak], atoms: dict[str, Atom], legal: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result = []
    assigned_atoms = {item["atom_id"] for items in legal.values() for item in items}
    for peak in peaks:
        if not legal.get(peak.peak_id):
            result.append({"kind": "peak_without_legal_candidate", "peak_id": peak.peak_id, "nucleus": peak.nucleus, "status": "unknown"})
        elif len(legal[peak.peak_id]) > 1:
            result.append({"kind": "ambiguous_peak", "peak_id": peak.peak_id, "legal_candidate_count": len(legal[peak.peak_id]), "status": "ambiguous"})
    for atom in atoms.values():
        if atom.shift is not None and atom.atom_id not in assigned_atoms:
            result.append({"kind": "predicted_nucleus_without_observed_peak", "atom_id": atom.atom_id, "nucleus": atom.nucleus, "status": "missing_peak"})
    return sorted(result, key=lambda item: (item["kind"], item.get("peak_id", ""), item.get("atom_id", "")))
