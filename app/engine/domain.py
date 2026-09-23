"""Deterministic NMR assignment candidate state machine.

The scoring model is intentionally transparent: it keeps every legal
candidate, rejection reason, tie and missing-edge diagnostic as data.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

ALGORITHM_VERSION = "nmr-assignment-candidates/2026.09"
DEFAULT_PARAMS: Dict[str, Any] = {
    "proton_shift_scale_ppm": 0.08,
    "carbon_shift_scale_ppm": 0.12,
    "overlap_tolerance_ppm": 0.03,
    "tie_tolerance": 0.08,
    "minimum_candidate_score": 0.35,
    "weights": {"shift": 0.62, "multiplicity": 0.10, "integral": 0.08, "network": 0.20},
    "proton_global_correction_ppm": 0.0,
    "carbon_global_correction_ppm": 0.0,
}
SCOPE_NUCLEUS = {"proton": "1H", "carbon": "13C"}
NUCLEUS_SCOPE = {v: k for k, v in SCOPE_NUCLEUS.items()}
MULTIPLICITY_SCORE = {"s": 1.0, "d": 0.9, "t": 0.8, "q": 0.8, "dd": 0.82, "dt": 0.78, "td": 0.78, "m": 0.55}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any, prefix: str = "fp") -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def default_params() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_PARAMS))


def merge_params(old: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    result = json.loads(json.dumps(old))
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = value
    return result


def _doc_index(documents: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], Dict[str, Any]]:
    by_id = {doc["id"]: doc for doc in documents}
    peak_owner: Dict[str, str] = {}
    skeleton = None
    for doc in documents:
        if doc["kind"] == "1d_spectrum":
            for peak in doc["parsed"]["peaks"]:
                peak_owner[peak["id"]] = doc["id"]
        if doc["kind"] == "molecular_skeleton":
            skeleton = doc["parsed"]
    if skeleton is None:
        raise ValueError("molecular_skeleton document is required")
    return by_id, peak_owner, skeleton


def source_fingerprint(documents: Sequence[Dict[str, Any]], scope: str) -> Dict[str, Any]:
    nucleus = SCOPE_NUCLEUS[scope]
    selected = []
    for doc in documents:
        if doc["kind"] == "1d_spectrum":
            include = doc["parsed"]["nucleus"] == nucleus
        elif doc["kind"] == "2d_correlation":
            include = nucleus in doc["parsed"]["nuclei"]
        else:
            include = True
        if include:
            selected.append({"id": doc["id"], "checksum": doc["checksum"]})
    return {"scope": scope, "documents": sorted(selected, key=lambda x: x["id"])}


def run_dependencies(documents: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {scope: source_fingerprint(documents, scope) for scope in ("proton", "carbon")}


def build_run(scope: str, documents: Sequence[Dict[str, Any]], parameters: Dict[str, Any], seed: int = 20260923) -> Dict[str, Any]:
    if scope not in SCOPE_NUCLEUS:
        raise ValueError(f"unknown scope {scope}")
    nucleus = SCOPE_NUCLEUS[scope]
    by_id, peak_owner, skeleton = _doc_index(documents)
    spectrum_doc = next((d for d in documents if d["kind"] == "1d_spectrum" and d["parsed"]["nucleus"] == nucleus), None)
    if spectrum_doc is None:
        raise ValueError(f"1D spectrum for {nucleus} is required")
    peaks = list(spectrum_doc["parsed"]["peaks"])
    targets = [n for n in skeleton["nuclei"] if n["observed_nucleus"] == nucleus]
    scale = float(parameters[f"{scope}_shift_scale_ppm"])
    correction = float(parameters.get(f"{scope}_global_correction_ppm", 0.0))
    tolerance = float(parameters["overlap_tolerance_ppm"])
    groups = _overlap_groups(peaks, tolerance)
    diagnostics = _global_diagnostics(peaks, targets, scale, scope)
    observed, expected = _edge_sets(scope, documents, skeleton, peak_owner)
    proton_peaks = [p for spec_d in documents if spec_d["kind"] == "1d_spectrum" and spec_d["parsed"]["nucleus"] == "1H" for p in spec_d["parsed"]["peaks"]]
    carbon_peaks = [p for spec_d in documents if spec_d["kind"] == "1d_spectrum" and spec_d["parsed"]["nucleus"] == "13C" for p in spec_d["parsed"]["peaks"]]
    proton_targets = [n for n in skeleton["nuclei"] if n["observed_nucleus"] == "1H"]
    carbon_targets = [n for n in skeleton["nuclei"] if n["observed_nucleus"] == "13C"]
    likely_h = _likely_targets(proton_peaks, proton_targets, float(parameters["proton_shift_scale_ppm"]), correction if scope == "proton" else 0.0)
    likely_c = _likely_targets(carbon_peaks, carbon_targets, float(parameters["carbon_shift_scale_ppm"]), correction if scope == "carbon" else 0.0)
    candidates: List[Dict[str, Any]] = []
    rejectors: List[Dict[str, Any]] = []
    for peak in peaks:
        if peak["shift"]["value"] is None:
            rejectors.append(_rejection(peak, None, "unconvertible_shift", spectrum_doc["id"], {}))
            continue
        adjusted_shift = float(peak["shift"]["value"]) + correction
        for target in targets:
            prior = target["prior_shift"]["value"]
            if prior is None:
                rejectors.append(_rejection(peak, target, "unconvertible_prior", spectrum_doc["id"], {}))
                continue
            residual = adjusted_shift - float(prior)
            shift_component = math.exp(-0.5 * (residual / scale) ** 2)
            multiplicity_component = _multiplicity_component(scope, peak, target)
            integral_component = _integral_component(scope, peak, target)
            network_component, edge_evidence = _network_component(
                scope, peak, target, skeleton, observed, expected, likely_h, likely_c
            )
            weights = parameters["weights"]
            score = (
                weights["shift"] * shift_component
                + weights["multiplicity"] * multiplicity_component
                + weights["integral"] * integral_component
                + weights["network"] * network_component
            )
            if score < float(parameters["minimum_candidate_score"]):
                rejectors.append(
                    _rejection(
                        peak,
                        target,
                        "below_minimum_score",
                        spectrum_doc["id"],
                        {
                            "score": round(score, 6),
                            "minimum": parameters["minimum_candidate_score"],
                            "residual_ppm": round(residual, 6),
                        },
                    )
                )
                continue
            candidates.append(
                {
                    "id": f"cand:{scope}:{peak['id']}->{target['id']}",
                    "scope": scope,
                    "nucleus": nucleus,
                    "peak_id": peak["id"],
                    "target_id": target["id"],
                    "status": "eligible",
                    "score": round(score, 6),
                    "components": {
                        "shift": round(shift_component, 6),
                        "multiplicity": round(multiplicity_component, 6),
                        "integral": round(integral_component, 6),
                        "network": round(network_component, 6),
                    },
                    "residual_ppm": round(residual, 6),
                    "adjusted_shift_ppm": round(adjusted_shift, 6),
                    "tie_group": None,
                    "ambiguity_tags": sorted(
                        set(_ambiguity_tags(peak, target, groups, edge_evidence, scope))
                    ),
                    "edge_evidence": edge_evidence,
                    "provenance": {
                        "algorithm_version": ALGORITHM_VERSION,
                        "source_document_id": spectrum_doc["id"],
                        "direct_source_ids": sorted({spectrum_doc["id"], *edge_evidence["source_document_ids"]}),
                        "parameter_fingerprint": fingerprint(parameters, "params"),
                    },
                    "seed": seed,
                }
            )
    candidates = _mark_ties(candidates, float(parameters["tie_tolerance"]))
    unobserved = _unobserved_targets(peaks, targets, candidates, spectrum_doc["id"])
    dep = source_fingerprint(documents, scope)
    parameter_fingerprint = fingerprint(parameters, "params")
    run_id = "run:" + fingerprint(
        {"scope": scope, "dependencies": dep, "params": parameters, "algorithm": ALGORITHM_VERSION, "seed": seed},
        scope,
    )[4:]
    return {
        "id": run_id,
        "scope": scope,
        "nucleus": nucleus,
        "algorithm_version": ALGORITHM_VERSION,
        "parameter_fingerprint": parameter_fingerprint,
        "parameters": parameters,
        "dependencies": dep,
        "candidates": candidates,
        "rejections": rejectors,
        "unobserved_targets": unobserved,
        "overlap_groups": groups,
        "missing_expected_edges": _serialize_edges(expected - observed),
        "observed_edges": _serialize_edges(observed),
        "diagnostics": diagnostics,
        "seed": seed,
        "state": "current",
    }


def _overlap_groups(peaks: Sequence[Dict[str, Any]], tolerance: float) -> List[Dict[str, Any]]:
    usable = [p for p in peaks if p["shift"]["value"] is not None]
    ordered = sorted(usable, key=lambda p: (p["shift"]["value"], p["id"]))
    groups: List[List[Dict[str, Any]]] = []
    declared = defaultdict(set)
    for peak in peaks:
        for other in peak.get("overlaps_with", []) or []:
            declared[peak["id"]].add(other)
            declared[other].add(peak["id"])
    for peak in ordered:
        placed = False
        for group in groups:
            if any(abs(peak["shift"]["value"] - member["shift"]["value"]) <= tolerance for member in group):
                group.append(peak)
                placed = True
                break
        if not placed:
            groups.append([peak])
    result = []
    for idx, group in enumerate(groups):
        explicit = set()
        members = {m["id"] for m in group}
        for pid in members:
            explicit.update(declared.get(pid, set()) & members)
        if len(group) > 1 or explicit:
            result.append(
                {
                    "id": "overlap:" + ",".join(sorted(members)),
                    "peak_ids": sorted(members),
                    "reason": "declared_overlap" if explicit else "shift_tolerance",
                    "resolution": "non_unique",
                }
            )
    return result


def _multiplicity_component(scope: str, peak: Dict[str, Any], target: Dict[str, Any]) -> float:
    if scope != "proton":
        return 1.0
    observed = peak.get("multiplicity", {}).get("code")
    expected = target.get("expected_multiplicity")
    if not observed or not expected:
        return 0.65 if observed is None else 0.8
    if observed == expected:
        return 1.0
    return max(0.35, MULTIPLICITY_SCORE.get(observed, 0.5) - 0.2)


def _integral_component(scope: str, peak: Dict[str, Any], target: Dict[str, Any]) -> float:
    if scope != "proton":
        return 1.0
    integral = peak.get("integral") or {}
    if integral.get("value") is None:
        return 0.7
    expected = 2 if target.get("symmetry_group") else 1
    observed = float(integral["value"])
    if abs(observed - expected) < 0.01:
        return 1.0
    return max(0.25, 1.0 - abs(observed - expected) / max(expected, 1))


def _bond_distance(skeleton: Dict[str, Any], left: str, right: str) -> Optional[int]:
    adjacency = defaultdict(set)
    for bond in skeleton["bonds"]:
        adjacency[bond["from"]].add(bond["to"])
        adjacency[bond["to"]].add(bond["from"])
    frontier = [(left, 0)]
    seen = {left}
    while frontier:
        node, dist = frontier.pop(0)
        if node == right:
            return dist
        for nxt in adjacency[node]:
            if nxt not in seen and dist < 8:
                seen.add(nxt)
                frontier.append((nxt, dist + 1))
    return None


def _likely_targets(peaks, targets, scale, correction=0.0):
    likely = defaultdict(set)
    for peak in peaks:
        if peak.get("shift", {}).get("value") is None:
            continue
        adjusted = float(peak["shift"]["value"]) + correction
        for target in targets:
            prior = target.get("prior_shift", {}).get("value")
            if prior is None:
                continue
            if abs(adjusted - float(prior)) <= scale * 2.0:
                likely[peak["id"]].add(target["id"])
    return likely


def _edge_sets(scope, documents, skeleton, peak_owner):
    observed = set()
    expected = set()
    protons = [n["id"] for n in skeleton["nuclei"] if n["observed_nucleus"] == "1H"]
    carbons = [n["id"] for n in skeleton["nuclei"] if n["observed_nucleus"] == "13C"]
    for doc in documents:
        if doc["kind"] != "2d_correlation" or SCOPE_NUCLEUS[scope] not in doc["parsed"]["nuclei"]:
            continue
        for edge in doc["parsed"]["correlations"]:
            observed.add((doc["parsed"]["experiment"], edge["from_peak"], edge["to_peak"], doc["id"]))
    for left in protons:
        for right in protons:
            if left < right and _bond_distance(skeleton, left, right) == 3:
                expected.add(("COSY", left, right, "expected"))
        for carbon in carbons:
            dist = _bond_distance(skeleton, left, carbon)
            if dist == 1:
                expected.add(("HSQC", left, carbon, "expected"))
            if dist in (2, 3):
                expected.add(("HMBC", left, carbon, "expected"))
    return observed, expected


def _edge_other(edge, peak_id):
    return edge[2] if edge[1] == peak_id else edge[1]


def _network_component(scope, peak, target, skeleton, observed_edges, expected_edges, likely_h, likely_c):
    source_ids = set()
    satisfied = 0
    relevant = 0
    contradiction = 0
    target_id = target["id"]
    target_nucleus = "1H" if scope == "proton" else "13C"
    for experiment, left, right, doc_id in observed_edges:
        if peak["id"] not in (left, right):
            continue
        other_peak = _edge_other((experiment, left, right, doc_id), peak["id"])
        compatible = False
        incompatible = False
        if experiment == "COSY" and target_nucleus == "1H":
            for other_target in likely_h.get(other_peak, set()):
                dist = _bond_distance(skeleton, target_id, other_target)
                compatible = compatible or dist == 3
                incompatible = incompatible or (dist is not None and dist > 4)
        elif experiment == "HSQC":
            if scope == "proton":
                attached = target.get("attached_to")
                compatible = attached is not None and attached in likely_c.get(other_peak, set())
                incompatible = bool(likely_c.get(other_peak)) and not compatible
            else:
                attached_h = {
                    n["id"] for n in skeleton["nuclei"]
                    if n.get("observed_nucleus") == "1H" and n.get("attached_to") == target_id
                }
                compatible = bool(attached_h & likely_h.get(other_peak, set()))
                incompatible = bool(likely_h.get(other_peak)) and not compatible
        elif experiment == "HMBC":
            other_likely = likely_c.get(other_peak, set()) if scope == "proton" else likely_h.get(other_peak, set())
            for other_target in other_likely:
                dist = _bond_distance(skeleton, target_id, other_target)
                compatible = compatible or dist in (2, 3)
                incompatible = incompatible or (dist is not None and dist > 4)
        if compatible or incompatible:
            source_ids.add(doc_id)
            relevant += 1
            if compatible:
                satisfied += 1
            if incompatible and not compatible:
                contradiction += 1
    missing_relevant = 0
    missing_details = []
    for experiment, left, right, _ in expected_edges:
        if target_id not in (left, right):
            continue
        other_target = right if left == target_id else left
        if experiment == "COSY" and scope != "proton":
            continue
        if experiment == "HSQC" and scope == "proton":
            required_other = target.get("attached_to")
            if required_other != other_target:
                continue
        if experiment == "HSQC" and scope == "carbon":
            attached_h = {
                n["id"] for n in skeleton["nuclei"]
                if n.get("observed_nucleus") == "1H" and n.get("attached_to") == target_id
            }
            if other_target not in attached_h:
                continue
        present = False
        for observed_experiment, observed_left, observed_right, _ in observed_edges:
            if observed_experiment != experiment or peak["id"] not in (observed_left, observed_right):
                continue
            other_peak = _edge_other((observed_experiment, observed_left, observed_right, ""), peak["id"])
            possible = (likely_h if experiment == "COSY" or (experiment == "HSQC" and scope == "carbon") or (experiment == "HMBC" and scope == "carbon") else likely_c).get(other_peak, set())
            if other_target in possible:
                present = True
                break
        if not present:
            missing_relevant += 1
            missing_details.append({"experiment": experiment, "other_target_id": other_target})
    support = 0.55 + min(0.35, 0.12 * satisfied) - min(0.20, 0.04 * missing_relevant) - 0.10 * contradiction
    return max(0.0, min(1.0, support)), {
        "satisfied_observed_edges": satisfied,
        "relevant_observed_edges": relevant,
        "relevant_missing_expected_edges": missing_relevant,
        "missing_edge_details": missing_details,
        "contradictory_edges": contradiction,
        "source_document_ids": sorted(source_ids),
    }


def _ambiguity_tags(peak, target, groups, edge_evidence, scope):
    tags = []
    if target.get("symmetry_group"):
        tags.append("symmetry_atom")
    if any(peak["id"] in group["peak_ids"] for group in groups):
        tags.append("overlapped_peak")
    if edge_evidence["relevant_missing_expected_edges"]:
        tags.append("missing_correlation_edge")
    if peak.get("shift", {}).get("value") is None or peak.get("intensity", {}).get("value") is None:
        tags.append("unconverted_original_quantity")
    return tags


def _mark_ties(candidates: Sequence[Dict[str, Any]], tie_tolerance: float) -> List[Dict[str, Any]]:
    by_peak: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_peak[candidate["peak_id"]].append(candidate)
    out = list(candidates)
    for peak_id, rows in by_peak.items():
        if not rows:
            continue
        best = max(row["score"] for row in rows)
        tied = [row for row in rows if abs(best - row["score"]) <= tie_tolerance]
        if len(tied) > 1:
            group_id = "tie:" + ",".join(sorted(row["target_id"] for row in tied))
            for row in tied:
                row["tie_group"] = group_id
                if "tied_optimum" not in row["ambiguity_tags"]:
                    row["ambiguity_tags"] = sorted(set(row["ambiguity_tags"] + ["tied_optimum"]))
    return sorted(out, key=lambda c: (c["peak_id"], -c["score"], c["target_id"]))


def _global_diagnostics(peaks, targets, scale, scope):
    residuals = []
    for peak in peaks:
        if peak["shift"]["value"] is None:
            continue
        priors = [t["prior_shift"]["value"] for t in targets if t["prior_shift"]["value"] is not None]
        if priors:
            residuals.append(float(peak["shift"]["value"]) - min(priors, key=lambda x: abs(float(peak["shift"]["value"]) - x)))
    diagnostics = []
    if residuals:
        median = float(np.median(np.array(residuals, dtype=float)))
        if abs(median) > scale * 0.75:
            diagnostics.append(
                {
                    "code": "global_shift_bias_suspected",
                    "scope": scope,
                    "median_residual_ppm": round(median, 6),
                    "suggested_correction_ppm": round(-median, 6),
                    "severity": "warning",
                    "rule": "median-nearest-prior-v0",
                }
            )
    return diagnostics


def _rejection(peak, target, reason, source_id, details):
    return {
        "id": f"reject:{peak['id']}->{target['id'] if target else 'none'}",
        "peak_id": peak["id"],
        "target_id": target["id"] if target else None,
        "reason": reason,
        "details": details,
        "direct_source_ids": [source_id],
    }


def _unobserved_targets(peaks, targets, candidates, source_id):
    observed_targets = {c["target_id"] for c in candidates}
    rows = []
    for target in targets:
        if target["id"] not in observed_targets:
            rows.append(
                {
                    "id": f"unknown:{target['id']}",
                    "target_id": target["id"],
                    "state": "unknown",
                    "reason": "no_peak_can_be_forced",
                    "symmetry_group": target.get("symmetry_group"),
                    "direct_source_ids": [source_id],
                }
            )
    return rows


def _serialize_edges(edges):
    return [
        {"experiment": experiment, "from_peak": left, "to_peak": right, "source_document_id": doc_id}
        for experiment, left, right, doc_id in sorted(edges)
    ]


def dependencies_match(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
    return canonical_json(old) == canonical_json(new)


def affected_scopes(dependencies_before: Dict[str, Any], dependencies_after: Dict[str, Any]) -> List[str]:
    affected = []
    for scope in ("proton", "carbon"):
        if not dependencies_match(dependencies_before.get(scope, {}), dependencies_after.get(scope, {})):
            affected.append(scope)
    return affected
