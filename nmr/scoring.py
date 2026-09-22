from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Tuple

import numpy as np

ALGORITHM_VERSION = "resonance-evidence-grid-1.0.0"


def default_parameters() -> Dict[str, Any]:
    return {
        "rule_version": "rules-v1",
        "seed": 260923,
        "proton_primary_tolerance_ppm": 0.25,
        "proton_broad_tolerance_ppm": 1.0,
        "carbon_primary_tolerance_ppm": 2.0,
        "carbon_broad_tolerance_ppm": 6.0,
        "shift_bias_ppm": {"1H": 0.0, "13C": 0.0},
        "evidence_weight_ppm": 0.03,
        "evidence_bonus": 0.08,
        "missing_edge_penalty": 0.0,
        "tie_rank_direction": "ascending",
    }


def canonical(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(",".join((canonical(k), canonical(value[k]))) for k in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    return repr(value)


def parameter_fingerprint(parameters: Dict[str, Any]) -> str:
    return "fp_" + hashlib.sha256(canonical(parameters).encode("utf-8")).hexdigest()[:16]


def _hydrogen_attachments(molecule: Dict[str, Any]) -> Dict[str, str]:
    return {a: str(atom["attached_to"]) for a, atom in molecule["atoms"].items()
            if atom.get("element") == "H" and atom.get("attached_to")}


def _bond_distance(molecule: Dict[str, Any]) -> Dict[Tuple[str, str], int]:
    atoms = list(molecule["atoms"])
    dist = {(a, a): 0 for a in atoms}
    for a, b, _ in molecule["bonds"]:
        dist[(a, b)] = 1
        dist[(b, a)] = 1
    for mid in atoms:
        for a in atoms:
            for b in atoms:
                if (a, mid) in dist and (mid, b) in dist:
                    dist[(a, b)] = min(dist.get((a, b), 10**9), dist[(a, mid)] + dist[(mid, b)])
    return dist


def _edge_index(parsed: Dict[str, Any], experiment: str) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    result: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for edge in parsed["documents"][experiment]["edges"]:
        result.setdefault((edge["from"], edge["to"]), []).append(edge)
        result.setdefault((edge["to"], edge["from"]), []).append(edge)
    return result


def _candidate_id(atom_id: str, peak_id: str) -> str:
    digest = hashlib.sha256(f"{atom_id}|{peak_id}|{ALGORITHM_VERSION}".encode()).hexdigest()[:12]
    return f"cand:{digest}"


def _unknown_id(atom_id: str) -> str:
    digest = hashlib.sha256(f"missing|{atom_id}|{ALGORITHM_VERSION}".encode()).hexdigest()[:12]
    return f"unknown:{digest}"


def metadata_dependencies_for_atom(atom: Dict[str, Any], parsed: Dict[str, Any]) -> List[str]:
    nucleus = atom["nucleus"]
    meta = parsed["documents"][nucleus]["metadata"]
    keys = [f"meta:{nucleus}:solvent:{meta['solvent']}"]
    if atom.get("temperature_sensitive"):
        keys.append(f"meta:{nucleus}:temperature_k:{meta['temperature_k']}")
    keys.append(f"meta:{nucleus}:reference_standard:{meta['reference_standard']}")
    return keys


def _symmetry_peers(molecule: Dict[str, Any], atom_id: str) -> List[str]:
    atom = molecule["atoms"][atom_id]
    group = atom.get("symmetry_group")
    if not group:
        return [atom_id]
    return [atom_id] + [part for part in group.split(":") if part != atom_id]


def _atom_peaks(parsed: Dict[str, Any], molecule: Dict[str, Any], atom_id: str, tolerance: float = 0.55) -> List[str]:
    atom = molecule["atoms"][atom_id]
    nucleus = atom["nucleus"]
    expected = float(atom["expected_shift_ppm"][nucleus])
    ids = []
    for peak in parsed["documents"][nucleus]["peaks"]:
        value = peak["shift"]["normalized"]
        if value is not None and abs(value - expected) <= tolerance:
            ids.append(peak["id"])
    return ids


def _shift(observed: float, expected: float, bias: float, weight: float) -> Tuple[float, float]:
    residual = observed + bias - expected
    return float(np.exp(-0.5 * (residual / max(weight, 1e-9)) ** 2)), residual


def _multiplicity_ok(atom: Dict[str, Any], peak: Dict[str, Any]) -> bool:
    expected = set(atom.get("expected_multiplicity", []))
    return not expected or peak["multiplicity"]["normalized"] in expected


def _add_evidence(evidence: List[Dict[str, Any]], atom_id: str, edge: Dict[str, Any], note: str, direct: bool) -> None:
    evidence.append({
        "stable_id": f"ev:{atom_id}:{edge['experiment']}:{edge['id']}",
        "atom_id": atom_id,
        "edge_id": edge["stable_id"],
        "experiment": edge["experiment"],
        "status": "observed",
        "note": note,
        "direct_source": direct,
    })


def _proton_evidence(atom_id, atom, peak, parsed, molecule, distances, attachments, cosy, hsqc, hmbc, params, evidence):
    score = 0.0
    refs = []
    attached = attachments.get(atom_id)
    if attached and molecule["atoms"].get(attached, {}).get("nucleus") == "13C":
        for peer_carbon in _symmetry_peers(molecule, attached):
            for carbon_peak in _atom_peaks(parsed, molecule, peer_carbon):
                for edge in hsqc.get((peak["id"], carbon_peak), []):
                    score += params["evidence_bonus"]
                    refs.append(edge["stable_id"])
                    _add_evidence(evidence, atom_id, edge, "HSQC maps the proton peak to the directly attached carbon class", True)
    proton_atoms = [a for a, item in molecule["atoms"].items() if item.get("nucleus") == "1H"]
    for other in proton_atoms:
        if distances.get((atom_id, other)) != 3:
            continue
        for other_peak in _atom_peaks(parsed, molecule, other):
            for edge in cosy.get((peak["id"], other_peak), []):
                score += params["evidence_bonus"]
                refs.append(edge["stable_id"])
                _add_evidence(evidence, atom_id, edge, "COSY supports a three-bond proton/proton relation", False)
    for carbon_id, carbon in molecule["atoms"].items():
        if carbon.get("element") != "C" or distances.get((atom_id, carbon_id)) not in (2, 3):
            continue
        for carbon_peak in _atom_peaks(parsed, molecule, carbon_id, tolerance=2.5):
            for edge in hmbc.get((peak["id"], carbon_peak), []):
                score += params["evidence_bonus"] / 2
                refs.append(edge["stable_id"])
                _add_evidence(evidence, atom_id, edge, "HMBC is supporting evidence for a 2J/3J carbon relation", False)
    return score, sorted(set(refs))


def _carbon_evidence(atom_id, atom, peak, parsed, molecule, attachments, hsqc, params, evidence):
    score = 0.0
    refs = []
    for hydrogen, carbon in attachments.items():
        if carbon not in _symmetry_peers(molecule, atom_id):
            continue
        for hydrogen_peak in _atom_peaks(parsed, molecule, hydrogen):
            for edge in hsqc.get((hydrogen_peak, peak["id"]), []):
                score += params["evidence_bonus"]
                refs.append(edge["stable_id"])
                _add_evidence(evidence, atom_id, edge, "HSQC supports this carbon through an attached proton", False)
    return score, sorted(set(refs))


def calculate_candidates(parsed: Dict[str, Any], parameters: Dict[str, Any]) -> Dict[str, Any]:
    params = {**default_parameters(), **parameters}
    fingerprint = parameter_fingerprint(params)
    molecule = parsed["molecule"]
    attachments = _hydrogen_attachments(molecule)
    distances = _bond_distance(molecule)
    cosy, hsqc, hmbc = (_edge_index(parsed, name) for name in ("COSY", "HSQC", "HMBC"))
    candidates: List[Dict[str, Any]] = []
    unknowns: List[Dict[str, Any]] = []
    evidence: List[Dict[str, Any]] = []

    for nucleus, primary_name, broad_name in (
        ("1H", "proton_primary_tolerance_ppm", "proton_broad_tolerance_ppm"),
        ("13C", "carbon_primary_tolerance_ppm", "carbon_broad_tolerance_ppm"),
    ):
        primary = params[primary_name]
        broad = params[broad_name]
        for atom_id in molecule["atom_order"]:
            atom = molecule["atoms"][atom_id]
            if atom.get("nucleus") != nucleus or nucleus not in atom.get("expected_shift_ppm", {}):
                continue
            expected = float(atom["expected_shift_ppm"][nucleus])
            eligible = []
            rejected = []
            for peak in parsed["documents"][nucleus]["peaks"]:
                observed = peak["shift"]["normalized"]
                if observed is None:
                    rejected.append({"stable_id": _candidate_id(atom_id, peak["stable_id"]), "atom_id": atom_id,
                                     "peak_id": peak["stable_id"], "source_peak_id": peak["id"], "nucleus": nucleus,
                                     "state": "rejected", "reason_code": "UNCONVERTIBLE_SHIFT",
                                     "reason": "raw shift retained but not normalized", "score": None,
                                     "dependencies": [peak["stable_id"], f"atom:{atom_id}"],
                                     "parameter_fingerprint": fingerprint, "algorithm_version": ALGORITHM_VERSION})
                    continue
                shift_score, residual = _shift(observed, expected, params["shift_bias_ppm"].get(nucleus, 0.0),
                                               params["evidence_weight_ppm"])
                relation_score, refs = (0.0, [])
                if nucleus == "1H":
                    relation_score, refs = _proton_evidence(atom_id, atom, peak, parsed, molecule, distances,
                                                            attachments, cosy, hsqc, hmbc, params, evidence)
                else:
                    relation_score, refs = _carbon_evidence(atom_id, atom, peak, parsed, molecule, attachments,
                                                            hsqc, params, evidence)
                reasons = []
                if abs(residual) > broad:
                    reasons.append(("SHIFT_OUTSIDE_BROAD_WINDOW", "chemical shift is outside the broad candidate window"))
                if nucleus == "1H" and not _multiplicity_ok(atom, peak):
                    reasons.append(("MULTIPLICITY_INCOMPATIBLE", "raw multiplicity is incompatible with this atom class"))
                common = {
                    "stable_id": _candidate_id(atom_id, peak["stable_id"]),
                    "atom_id": atom_id,
                    "peak_id": peak["stable_id"],
                    "source_peak_id": peak["id"],
                    "nucleus": nucleus,
                    "observed_shift_ppm": observed,
                    "expected_shift_ppm": expected,
                    "shift_reference": atom.get("expected_shift_reference", "case-baseline"),
                    "residual_ppm": round(residual, 6),
                    "shift_score": round(shift_score, 6),
                    "evidence_score": round(relation_score, 6),
                    "score": round(shift_score + relation_score, 6),
                    "evidence_edge_ids": refs,
                    "missing_evidence_notes": [],
                    "overlap_group": peak.get("overlap_group"),
                    "symmetry_group": atom.get("symmetry_group"),
                    "direct_sources": [peak["stable_id"]],
                    "dependencies": sorted([peak["stable_id"], f"atom:{atom_id}", *metadata_dependencies_for_atom(atom, parsed)]),
                    "parameter_fingerprint": fingerprint,
                    "algorithm_version": ALGORITHM_VERSION,
                }
                if reasons:
                    rejected.append({**common, "state": "rejected", "reason_code": reasons[0][0], "reason": "; ".join(r[1] for r in reasons)})
                elif abs(residual) <= primary:
                    eligible_item = {**common, "state": "eligible", "reason_code": None, "reason": None}
                    if nucleus == "1H" and atom.get("exchangeable"):
                        eligible_item["assignment_constraint"] = "exchangeable-not-forced"
                    eligible.append(eligible_item)
                else:
                    rejected.append({**common, "state": "rejected", "reason_code": "SHIFT_OUTSIDE_PRIMARY_WINDOW",
                                     "reason": "shift lies between primary and broad windows"})
            if not eligible and atom.get("exchangeable"):
                unknowns.append({
                    "stable_id": _unknown_id(atom_id),
                    "atom_id": atom_id,
                    "nucleus": nucleus,
                    "state": "unknown",
                    "reason_code": "EXCHANGEABLE_PEAK_MISSING",
                    "reason": "the OH resonance is not uniquely observable in this source; all legal possibilities remain open",
                    "dependencies": sorted([f"atom:{atom_id}", *metadata_dependencies_for_atom(atom, parsed)]),
                    "parameter_fingerprint": fingerprint,
                    "algorithm_version": ALGORITHM_VERSION,
                })
            candidates.extend(eligible)
            candidates.extend(rejected)

    atom_options: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in candidates:
        if candidate["state"] == "eligible":
            atom_options.setdefault(candidate["atom_id"], []).append(candidate)
    symmetry_members = {tuple(group) for group in molecule.get("symmetry_groups", [])}
    symmetric_atoms = {atom for group in symmetry_members for atom in group if molecule["atoms"].get(atom, {}).get("nucleus")}
    symmetry_group_by_atom = {atom: group for group in symmetry_members for atom in group}
    for atom_id, options in atom_options.items():
        ordered = sorted(options, key=lambda item: (-item["score"], item["peak_id"]))
        top_score = ordered[0]["score"]
        peer_tie = atom_id in symmetric_atoms
        if peer_tie:
            group = symmetry_group_by_atom[atom_id]
            peer_best = []
            for peer in group:
                peer_options = atom_options.get(peer, [])
                peer_best.append(max((item["score"] for item in peer_options), default=-1.0))
            peer_tie = max(peer_best) == top_score and len(group) > 1
        rank = 1
        for candidate in ordered:
            candidate["rank"] = rank if candidate["score"] == top_score else None
            local_tie = candidate["score"] == top_score and sum(x["score"] == top_score for x in ordered) > 1
            candidate["tied_best"] = bool(local_tie or (peer_tie and candidate["score"] == top_score))
            if candidate.get("assignment_constraint") == "exchangeable-not-forced":
                candidate["ambiguity"] = "exchangeable-not-forced"
            elif candidate["tied_best"]:
                candidate["ambiguity"] = "tied-best"
            elif candidate.get("overlap_group"):
                candidate["ambiguity"] = "overlap-not-forced"
            else:
                candidate["ambiguity"] = None
            if candidate["score"] != top_score:
                rank += 1

    run_material = {"algorithm": ALGORITHM_VERSION, "parameters": fingerprint,
                    "sources": parsed["project"].get("source_files", []), "seed": params["seed"]}
    run_id = "run_" + hashlib.sha256(canonical(run_material).encode()).hexdigest()[:16]
    return {"run_id": run_id, "algorithm_version": ALGORITHM_VERSION, "parameters": params,
            "parameter_fingerprint": fingerprint, "candidates": candidates, "unknowns": unknowns,
            "evidence": evidence}
