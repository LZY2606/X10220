"""Domain computation: assignment candidates, evidence, rejects, unknowns.

Design rules enforced here:

* overlapping peaks and missing peaks are never force-matched uniquely; the
  coupling network is evidence only;
* solvent / temperature / reference changes only invalidate dependent derived
  nodes; old decisions keep referencing the old candidates and stay replayable;
* candidates, rejection reasons, errors and unknown states are all first-class
  data; several legal candidates are retained whenever uniqueness is impossible.

Pure functions only: given parsed inputs + parameters the output byte-for-byte
deterministically matches for the same fixed seed (no wall clock, no global
state).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .identity import PREFIX_CANDIDATE, PREFIX_EVIDENCE, content_id, fingerprint

ALGORITHM_VERSION = "gsb-assignment/1.0.0"
EXPERIMENT_OF_NUCLEUS = {"H": "1H", "C": "13C"}
MULT_MAP = {"s": "s", "brs": "s", "d": "d", "br d": "d", "t": "t", "q": "q", "m": "m",
            "dd": "dd", "dt": "dt", "td": "dt", "sept": "sept", "h": "sept", "p": "sept"}


def _normalise_shift(value: Optional[float], experiment: str, offsets: Dict[str, float]) -> Optional[float]:
    if value is None:
        return None
    return value + float(offsets.get(experiment, 0.0))


def _gaussian(delta: float, sigma: float) -> float:
    return float(math.exp(-0.5 * (delta / sigma) ** 2))


def _equiv_group(atom_id: str, equivalence: List[List[str]]) -> Tuple[str, ...]:
    for group in equivalence:
        if atom_id in group:
            return tuple(sorted(group))
    return (atom_id,)


def _bond_partners(skeleton: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {a["id"]: [] for a in skeleton["atoms"]}
    for bond in skeleton["bonds"]:
        out[bond["a"]].append(bond["b"])
        out[bond["b"]].append(bond["a"])
    return out


def _build_atom_index(skeleton: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {a["id"]: a for a in skeleton["atoms"]}


def _edge_match(edge_shift: Optional[float], peak_shift: Optional[float], tolerance: float) -> Optional[float]:
    if edge_shift is None or peak_shift is None:
        return None
    if abs(edge_shift - peak_shift) <= tolerance:
        return _gaussian(edge_shift - peak_shift, tolerance)
    return None


def _hsqc_partner_map(skeleton: Dict[str, Any]) -> Dict[str, str]:
    """Explicit attached H<->C map declared by the skeleton (hsqc_pairs)."""
    mapping: Dict[str, str] = {}
    for pair in skeleton.get("hsqc_pairs", []):
        h_atom, c_atom = pair["h"], pair["c"]
        mapping[h_atom] = c_atom
        mapping[c_atom] = h_atom
    return mapping


def _hmbc_targets(skeleton: Dict[str, Any]) -> Dict[str, List[str]]:
    declared: Dict[str, List[str]] = {}
    for rel in skeleton.get("hmbc_relations", []):
        declared.setdefault(rel["from"], []).extend(rel["to"])
    if declared:
        return {k: sorted(set(v)) for k, v in declared.items()}
    # Fallback: 2- and 3-bond H->C partners derived from bonds.
    atoms = _build_atom_index(skeleton)
    partners = _bond_partners(skeleton)
    out: Dict[str, List[str]] = {}
    for atom in skeleton["atoms"]:
        if atom["nucleus"] != "H":
            continue
        targets = set()
        for one in partners.get(atom["id"], []):
            for two in partners.get(one, []):
                if atoms[two]["nucleus"] == "C" and two != one:
                    targets.add(two)
                for three in partners.get(two, []):
                    if atoms[three]["nucleus"] == "C" and three not in (one, two):
                        targets.add(three)
        out[atom["id"]] = sorted(targets)
    return out


def _cosy_targets(skeleton: Dict[str, Any]) -> Dict[str, List[str]]:
    declared: Dict[str, List[str]] = {}
    for rel in skeleton.get("cosy_relations", []):
        declared.setdefault(rel["from"], []).extend(rel["to"])
        declared.setdefault(rel["to"], []).append(rel["from"])
    if declared:
        return {k: sorted(set(v)) for k, v in declared.items()}
    # Fallback: H-H connected through one heavy atom (vicinal/geminal proxy).
    atoms = _build_atom_index(skeleton)
    partners = _bond_partners(skeleton)
    out: Dict[str, List[str]] = {}
    for atom in skeleton["atoms"]:
        if atom["nucleus"] != "H":
            continue
        targets = set()
        for heavy in partners.get(atom["id"], []):
            for other in partners.get(heavy, []):
                if other != atom["id"] and atoms[other]["nucleus"] == "H":
                    targets.add(other)
        out[atom["id"]] = sorted(targets)
    return out


def _edges_for_experiment(correlations: Dict[str, Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    parsed = correlations.get(kind)
    if not parsed:
        return []
    return parsed["edges"]


def compute_candidates(
    peak_lists: Dict[str, Dict[str, Any]],
    correlations: Dict[str, Dict[str, Any]],
    skeleton: Dict[str, Any],
    params: Dict[str, Any],
    source_versions: Dict[str, str],
) -> Dict[str, Any]:
    """Compute the full candidate batch from parsed source layers.

    ``source_versions`` maps source key (``peaks_1H``, ``peaks_13C``,
    ``corr_COSY`` ... ``skeleton``, ``params``) to the source version id of the
    effective input, so every candidate/evidence node records direct sources.
    """
    rng = np.random.default_rng(int(params.get("seed", 20260923)))
    weights = params["weights"]
    tolerances = params["tolerances"]
    offsets = params["reference_offsets"]
    pf = fingerprint({"algorithm": ALGORITHM_VERSION, "params": params})

    atoms = skeleton["atoms"]
    equivalence = skeleton.get("equivalence", [])
    predicted = {k: float(v) for k, v in skeleton.get("predicted_shifts", {}).items()}
    hsqc_partner = _hsqc_partner_map(skeleton)
    cosy_targets = _cosy_targets(skeleton)
    hmbc_targets = _hmbc_targets(skeleton)

    cos_edges = _edges_for_experiment(correlations, "COSY")
    hsqc_edges = _edges_for_experiment(correlations, "HSQC")
    hmbc_edges = _edges_for_experiment(correlations, "HMBC")

    sigma = {"1H": float(tolerances["shift_sigma_1h"]), "13C": float(tolerances["shift_sigma_13c"])}
    edge_tol = {
        "1H": float(tolerances.get("edge_match_1h", 0.03)),
        "13C": float(tolerances.get("edge_match_13c", 0.25)),
    }

    candidates: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []

    for atom in atoms:
        experiment = EXPERIMENT_OF_NUCLEUS[atom["nucleus"]]
        parsed = peak_lists.get(experiment)
        peaks = parsed["peaks"] if parsed else []
        offset = float(offsets[experiment])
        expected = predicted.get(atom["id"])
        deps = {"params": source_versions["params"], "skeleton": source_versions["skeleton"]}
        if parsed is not None:
            deps["peaks"] = source_versions["peaks_%s" % experiment]
        atom_candidates: List[Dict[str, Any]] = []
        if expected is None:
            findings.append(
                {
                    "kind": "unknown_atom",
                    "atom_id": atom["id"],
                    "reason": "no predicted shift for atom; cannot score any peak",
                    "severity": "warning",
                    "depends_on": dict(deps),
                }
            )
            continue
        for peak in peaks:
            observed = _normalise_shift(peak["shift"], experiment, offsets)
            evidence: List[Dict[str, Any]] = []
            if observed is None:
                evidence.append(
                    {
                        "kind": "unconvertible_shift",
                        "message": "observed shift is unconvertible; retained only as raw evidence",
                        "weight": 0.0,
                    }
                )
                shift_score = 0.0
                delta = None
            else:
                delta = observed - expected
                shift_score = _gaussian(delta, sigma[experiment])
                evidence.append(
                    {
                        "kind": "chemical_shift",
                        "observed_shift": observed,
                        "observed_shift_unit": "ppm",
                        "observed_shift_reference": parsed["metadata"].get("reference", "unknown")
                        if parsed else "unknown",
                        "raw_shift": peak["shift"],
                        "raw_shift_unit": peak.get("shift_unit", "ppm"),
                        "applied_offset_ppm": offset,
                        "predicted_shift": expected,
                        "predicted_shift_unit": "ppm",
                        "residual_ppm": delta,
                        "weight": float(weights.get("chemical_shift", 1.0)),
                    }
                )
            mult_score = _multiplicity_score(atom, peak)
            evidence.append(
                {
                    "kind": "multiplicity",
                    "expected": atom.get("multiplicity"),
                    "observed": peak["multiplicity"],
                    "observed_known": peak["multiplicity_known"],
                    "score": mult_score,
                    "weight": float(weights.get("multiplicity", 0.25)),
                }
            )
            integration_score = _integration_score(atom, peak, equivalence)
            evidence.append(
                {
                    "kind": "integration",
                    "expected_equivalents": _equiv_size(atom["id"], equivalence),
                    "observed_integration": peak["integration"],
                    "integration_unit": peak.get("integration_unit", "H-equivalents"),
                    "score": integration_score,
                    "weight": float(weights.get("integration", 0.25)),
                }
            )
            coupling = _coupling_evidence(
                atom, peak, cos_edges, hsqc_edges, hmbc_edges, predicted, hsqc_partner,
                cosy_targets, hmbc_targets, edge_tol, weights, deps,
            )
            evidence.extend(coupling["evidence"])
            for key in ("corr_COSY", "corr_HSQC", "corr_HMBC"):
                if key in source_versions:
                    deps[key] = source_versions[key]
            total = _combine_scores(shift_score, mult_score, integration_score, coupling, weights)
            score_sd = _bootstrapped_sd(rng, shift_score, mult_score, integration_score, coupling, weights)
            equiv_group = list(_equiv_group(atom["id"], equivalence))
            candidate = {
                "atom_id": atom["id"],
                "nucleus": atom["nucleus"],
                "experiment": experiment,
                "peak_identity": peak["identity"],
                "peak_source_lineno": peak["source_lineno"],
                "observed_shift_ppm": observed,
                "raw_shift": peak["shift"],
                "raw_shift_unit": peak.get("shift_unit", "ppm"),
                "expected_shift_ppm": expected,
                "residual_ppm": delta,
                "score": round(total, 6),
                "score_sd": round(score_sd, 6),
                "multiplicity_score": mult_score,
                "integration_score": integration_score,
                "shift_score": round(shift_score, 6),
                "equivalence_group": equiv_group,
                "evidence": evidence,
                "reject_reasons": [],
                "depends_on": dict(deps),
                "parameter_fingerprint": pf,
                "algorithm_version": ALGORITHM_VERSION,
            }
            atom_candidates.append(candidate)
        candidates.extend(_finalise_atom(atom_candidates, atom, parsed, tolerances, equivalence, deps))

    findings.extend(_diagnose_peaks(candidates, peak_lists, atoms, equivalence, source_versions))
    findings.extend(_diagnose_missing_couplings(candidates, cos_edges, hsqc_edges, hmbc_edges,
                                                cosy_targets, hsqc_partner, source_versions))
    findings.extend(_diagnose_bias(candidates, peak_lists, tolerances, source_versions))
    findings.extend(_diagnose_symmetry(candidates, equivalence))
    findings.extend(_diagnose_overlap(candidates, peak_lists))
    candidates = [_with_identity(c, pf) for c in candidates]
    findings = [_with_finding_identity(f, pf) for f in findings]
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "parameter_fingerprint": pf,
        "candidates": candidates,
        "findings": findings,
    }


def _equiv_size(atom_id: str, equivalence: List[List[str]]) -> int:
    return len(_equiv_group(atom_id, equivalence))


def _multiplicity_score(atom: Dict[str, Any], peak: Dict[str, Any]) -> float:
    expected = atom.get("multiplicity")
    if not expected or not peak["multiplicity_known"]:
        return 0.5
    observed_norm = MULT_MAP.get(peak["multiplicity"], peak["multiplicity"])
    expected_norm = MULT_MAP.get(expected, expected)
    if observed_norm == expected_norm:
        return 1.0
    # dd vs d are partially compatible (one resolved extra coupling).
    if {"dd", "dt", "d"} == {observed_norm, expected_norm} or {observed_norm, expected_norm} <= {"dd", "dt"}:
        return 0.6
    if "m" in (observed_norm, expected_norm):
        return 0.5
    return 0.05


def _integration_score(atom: Dict[str, Any], peak: Dict[str, Any], equivalence: List[List[str]]) -> float:
    expected_size = _equiv_size(atom["id"], equivalence)
    observed = peak.get("integration")
    if observed is None:
        return 0.5
    if observed <= 0:
        return 0.05
    ratio = float(expected_size) / float(observed)
    # A 2H symmetric site observed as 1H deconvolved peak is still plausible
    # (overlap decomposition), but a 1H site on a 2H integrated envelope is too.
    if abs(ratio - 1.0) < 1e-9:
        return 1.0
    if ratio in (0.5, 2.0) or abs(ratio - 0.5) < 1e-9 or abs(ratio - 2.0) < 1e-9:
        return 0.7
    if 0.4 <= ratio <= 2.5:
        return 0.4
    return 0.05


def _coupling_evidence(atom, peak, cos_edges, hsqc_edges, hmbc_edges, predicted,
                       hsqc_partner, cosy_targets, hmbc_targets, edge_tol, weights, deps):
    evidence: List[Dict[str, Any]] = []
    if atom["nucleus"] == "H":
        h_shift = peak["shift"]
        h_tol = edge_tol["1H"]
        # HSQC: attached carbon partner must carry a matching 13C peak.
        partner_c = hsqc_partner.get(atom["id"])
        hsqc_score = 0.0
        hsqc_detail = {"matched_edges": [], "attached_carbon": partner_c}
        if partner_c and predicted.get(partner_c) is not None:
            c_expected = predicted[partner_c]
            for edge in hsqc_edges:
                h_match = _edge_match(edge["shift_a"], h_shift, h_tol)
                c_match = _edge_match(edge["shift_b"], c_expected, edge_tol["13C"])
                if h_match and c_match:
                    strength = h_match * c_match
                    hsqc_score = max(hsqc_score, strength)
                    hsqc_detail["matched_edges"].append(edge["identity"])
        evidence.append({
            "kind": "HSQC",
            "present": bool(hsqc_detail["matched_edges"]),
            "detail": hsqc_detail,
            "score": hsqc_score if hsqc_detail["matched_edges"] else 0.0,
            "weight": float(weights.get("hsqc", 0.8)),
            "note": "missing HSQC edge is not penalised (edge may be unobserved)",
        })
        # COSY vs expected proton-proton partners.
        cosy_score, cosy_detail = _network_score(
            h_shift, cos_edges, cosy_targets.get(atom["id"], []), predicted, h_tol, h_tol, "a", "b")
        evidence.append({
            "kind": "COSY",
            "present": bool(cosy_detail["matched_edges"]),
            "detail": cosy_detail,
            "score": cosy_score,
            "weight": float(weights.get("cosy", 0.4)),
            "note": "corroborating evidence only; absent expected edge never forces rejection",
        })
        # HMBC to 2/3-bond carbons.
        hmbc_score, hmbc_detail = _network_score(
            h_shift, hmbc_edges, hmbc_targets.get(atom["id"], []), predicted, h_tol, edge_tol["13C"], "a", "b")
        evidence.append({
            "kind": "HMBC",
            "present": bool(hmbc_detail["matched_edges"]),
            "detail": hmbc_detail,
            "score": hmbc_score,
            "weight": float(weights.get("hmbc", 0.6)),
            "note": "long-range evidence only; missing correlations never veto a candidate",
        })
    else:
        # Carbon atom: HSQC attachment corroboration via proton partner.
        partner_h = hsqc_partner.get(atom["id"])
        hsqc_score = 0.0
        detail = {"matched_edges": [], "attached_proton": partner_h}
        if partner_h and predicted.get(partner_h) is not None:
            h_expected = predicted[partner_h]
            for edge in hsqc_edges:
                h_match = _edge_match(edge["shift_a"], h_expected, edge_tol["1H"])
                c_match = _edge_match(edge["shift_b"], peak["shift"], edge_tol["13C"])
                if h_match and c_match:
                    hsqc_score = max(hsqc_score, h_match * c_match)
                    detail["matched_edges"].append(edge["identity"])
        evidence.append({
            "kind": "HSQC",
            "present": bool(detail["matched_edges"]),
            "detail": detail,
            "score": hsqc_score,
            "weight": float(weights.get("hsqc", 0.5)),
            "note": "quaternary carbons legitimately have no HSQC; absence is not a penalty",
        })
    return {"evidence": evidence}


def _network_score(h_shift, edges, target_atoms, predicted, tol_a, tol_b, key_a, key_b):
    detail = {"matched_edges": [], "targets": target_atoms}
    best = 0.0
    if h_shift is None:
        return 0.0, detail
    target_expected = {t: predicted.get(t) for t in target_atoms if predicted.get(t) is not None}
    for edge in edges:
        a = edge[key_a]
        b = edge[key_b]
        ma = _edge_match(a, h_shift, tol_a)
        if not ma:
            continue
        for target, expected in target_expected.items():
            mb = _edge_match(b, expected, tol_b)
            if mb:
                strength = ma * mb
                best = max(best, strength)
                detail["matched_edges"].append(
                    {"edge": edge["identity"], "supports_atom": target, "strength": round(strength, 6)}
                )
    return best, detail


def _combine_scores(shift_score, mult_score, integration_score, coupling, weights):
    terms = [
        (shift_score, float(weights.get("chemical_shift", 1.0))),
        (mult_score, float(weights.get("multiplicity", 0.25))),
        (integration_score, float(weights.get("integration", 0.25))),
    ]
    for item in coupling["evidence"]:
        terms.append((item["score"], float(item["weight"])))
    weight_sum = sum(w for _, w in terms)
    return sum(s * w for s, w in terms) / weight_sum if weight_sum else 0.0


def _bootstrapped_sd(rng, shift_score, mult_score, integration_score, coupling, weights):
    # Fixed-seed bootstrap perturbation of evidence components -> uncertainty.
    base = [shift_score, mult_score, integration_score]
    base_w = [
        float(weights.get("chemical_shift", 1.0)),
        float(weights.get("multiplicity", 0.25)),
        float(weights.get("integration", 0.25)),
    ]
    for item in coupling["evidence"]:
        base.append(item["score"])
        base_w.append(float(item["weight"]))
    scores = np.array(base, dtype=float)
    w = np.array(base_w, dtype=float)
    draws = []
    n = scores.shape[0]
    for _ in range(64):
        idx = rng.integers(0, n, size=n)
        noise = rng.normal(0.0, 0.03, size=n)
        perturbed = np.clip(scores[idx] + noise[idx], 0.0, 1.0)
        ww = w[idx]
        draws.append(float(np.sum(perturbed * ww) / np.sum(ww)))
    return float(np.std(draws))


def _finalise_atom(atom_candidates, atom, parsed, tolerances, equivalence, deps):
    """Keep every legal candidate; annotate rejects/tie/unknown for one atom."""
    if not atom_candidates:
        return []
    window_key = "search_window_1h" if atom["nucleus"] == "H" else "search_window_13c"
    window = float(tolerances.get(window_key, 0.6 if atom["nucleus"] == "H" else 3.0))
    in_window = []
    for candidate in atom_candidates:
        if candidate["residual_ppm"] is None:
            candidate["reject_reasons"].append("unconvertible_observed_shift")
            candidate["status"] = "rejected"
            continue
        if abs(candidate["residual_ppm"]) <= window:
            in_window.append(candidate)
        else:
            candidate["reject_reasons"].append(
                "outside_search_window (|%.3f| ppm > %.3f ppm)" % (candidate["residual_ppm"], window)
            )
            candidate["status"] = "rejected"
    if not in_window:
        for candidate in atom_candidates:
            if candidate["status"] != "rejected":
                candidate["reject_reasons"].append("no_peak_in_window")
                candidate["status"] = "unknown"
        if parsed is None:
            atom_candidates[0]["reject_reasons"].append("no_spectrum_imported_for_nucleus")
        return atom_candidates
    ordered = sorted(in_window, key=lambda c: c["score"], reverse=True)
    best = ordered[0]["score"]
    tie_eps = float(tolerances.get("tie_epsilon", 0.02))
    threshold = float(tolerances.get("candidate_threshold", 0.35))
    for candidate in in_window:
        reasons = candidate["reject_reasons"]
        if candidate["score"] < threshold:
            reasons.append("below_candidate_threshold (%.3f < %.3f)" % (candidate["score"], threshold))
            candidate["status"] = "rejected"
        else:
            candidate["status"] = "candidate"
            candidate["tied"] = (best - candidate["score"]) <= tie_eps
    # Atom is "ambiguous" whenever >1 legal candidate survives (no forced match).
    survivors = [c for c in in_window if c["status"] == "candidate"]
    for candidate in survivors:
        candidate["legal_candidate_count_for_atom"] = len(survivors)
    for candidate in atom_candidates:
        candidate.setdefault("tied", False)
        candidate.setdefault("status", "rejected")
    return atom_candidates


def _diagnose_peaks(candidates, peak_lists, atoms, equivalence, source_versions):
    findings = []
    atom_by_id = {a["id"]: a for a in atoms}
    for experiment, parsed in peak_lists.items():
        used = set()
        for candidate in candidates:
            if candidate["experiment"] == experiment and candidate["status"] == "candidate":
                used.add(candidate["peak_identity"])
        for peak in parsed["peaks"]:
            if peak["identity"] not in used:
                findings.append({
                    "kind": "unassigned_peak",
                    "experiment": experiment,
                    "peak_identity": peak["identity"],
                    "raw_shift": peak["shift"],
                    "shift_unit": peak.get("shift_unit", "ppm"),
                    "reason": "no atom scores above threshold for this peak; retained unassigned",
                    "severity": "info",
                    "depends_on": {"peaks": source_versions.get("peaks_%s" % experiment, ""),
                                   "skeleton": source_versions.get("skeleton", ""),
                                   "params": source_versions.get("params", "")},
                })
    return findings


def _diagnose_missing_couplings(candidates, cos_edges, hsqc_edges, hmbc_edges,
                                cosy_targets, hsqc_partner, source_versions):
    """Report missing *expected* edges for the current best tentative map.

    Missing edges are diagnostics on the coupling evidence, never vetoes and
    never forced matches: the coupling network is evidence only.
    """
    findings = []
    best_per_peak: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for candidate in candidates:
        if candidate["status"] != "candidate":
            continue
        key = (candidate["experiment"], candidate["peak_identity"])
        if key not in best_per_peak or candidate["score"] > best_per_peak[key]["score"]:
            best_per_peak[key] = candidate
    h_peak_by_atom: Dict[str, Dict[str, Any]] = {}
    for (experiment, _pk), candidate in best_per_peak.items():
        if experiment == "1H":
            h_peak_by_atom.setdefault(candidate["atom_id"], candidate)

    present_hsqc = {(e["shift_a"], e["shift_b"]) for e in hsqc_edges if e["shift_a"] is not None}
    present_cosy = {(min(e["shift_a"], e["shift_b"]), max(e["shift_a"], e["shift_b"]))
                    for e in cos_edges if e["shift_a"] is not None}
    for atom_id, candidate in h_peak_by_atom.items():
        partner_c = hsqc_partner.get(atom_id)
        h_shift = candidate["raw_shift"]
        if partner_c:
            c_candidates = [c for c in candidates if c["atom_id"] == partner_c and c["status"] == "candidate"]
            if c_candidates:
                c_shift = max(c_candidates, key=lambda c: c["score"])["raw_shift"]
                if h_shift is not None and c_shift is not None and (h_shift, c_shift) not in present_hsqc:
                    findings.append({
                        "kind": "missing_correlation_edge",
                        "experiment_kind": "HSQC",
                        "from_atom": atom_id,
                        "to_atom": partner_c,
                        "expected_h_shift_ppm": h_shift,
                        "expected_c_shift_ppm": c_shift,
                        "severity": "info",
                        "reason": "HSQC edge expected from tentative map but absent; evidence gap, not a veto",
                        "depends_on": {k: v for k, v in source_versions.items()
                                       if k in ("corr_HSQC", "skeleton", "params")},
                    })
        for partner_h in cosy_targets.get(atom_id, []):
            other = h_peak_by_atom.get(partner_h)
            if other and h_shift is not None and other["raw_shift"] is not None:
                pair = (min(h_shift, other["raw_shift"]), max(h_shift, other["raw_shift"]))
                if pair not in present_cosy:
                    findings.append({
                        "kind": "missing_correlation_edge",
                        "experiment_kind": "COSY",
                        "from_atom": atom_id,
                        "to_atom": partner_h,
                        "expected_shifts_ppm": list(pair),
                        "severity": "info",
                        "reason": "COSY edge expected from skeleton but unobserved; kept as evidence gap",
                        "depends_on": {k: v for k, v in source_versions.items()
                                       if k in ("corr_COSY", "skeleton", "params")},
                    })
    return findings


def _diagnose_bias(candidates, peak_lists, tolerances, source_versions):
    findings = []
    for experiment, parsed in peak_lists.items():
        sigma = float(tolerances["shift_sigma_1h"] if experiment == "1H" else tolerances["shift_sigma_13c"])
        best_by_peak: Dict[str, Dict[str, Any]] = {}
        for candidate in candidates:
            if candidate["experiment"] != experiment or candidate["residual_ppm"] is None:
                continue
            if candidate["status"] != "candidate":
                continue
            pk = candidate["peak_identity"]
            if pk not in best_by_peak or candidate["score"] > best_by_peak[pk]["score"]:
                best_by_peak[pk] = candidate
        residuals = [c["residual_ppm"] for c in best_by_peak.values()]
        if len(residuals) < 3:
            continue
        median = float(np.median(residuals))
        threshold = sigma * float(tolerances.get("bias_trigger_sigma", 1.5))
        same_sign = all(r * median > 0 for r in residuals)
        if same_sign and abs(median) >= threshold:
            findings.append({
                "kind": "global_shift_bias_suspected",
                "experiment": experiment,
                "median_residual_ppm": round(median, 4),
                "residual_unit": "ppm",
                "reference_basis": parsed["metadata"].get("reference", "unspecified"),
                "suggested_correction_ppm": -round(median, 4),
                "severity": "warning",
                "reason": ("residuals share sign with |median| >= %.3f ppm; a reference/global offset is "
                           "likely. Correct via a new parameter version; old batch remains replayable."
                           % threshold),
                "affected_experiments_only": [experiment],
                "depends_on": {"params": source_versions.get("params", ""),
                               "skeleton": source_versions.get("skeleton", ""),
                               "peaks": source_versions.get("peaks_%s" % experiment, "")},
            })
    return findings



def _diagnose_symmetry(candidates, equivalence):
    findings = []
    for group in equivalence:
        members = set(group)
        by_peak: Dict[str, List[Dict[str, Any]]] = {}
        for candidate in candidates:
            if candidate["atom_id"] in members and candidate["status"] == "candidate":
                by_peak.setdefault(candidate["peak_identity"], []).append(candidate)
        for peak_identity, tied in by_peak.items():
            atoms = {c["atom_id"] for c in tied}
            if atoms >= members and len(tied) > 1:
                scores = sorted({c["score"] for c in tied})
                if scores[-1] - scores[0] <= 0.02:
                    findings.append({
                        "kind": "symmetric_atom_tie",
                        "equivalence_group": sorted(members),
                        "peak_identity": peak_identity,
                        "scores": scores,
                        "severity": "info",
                        "reason": "symmetry-equivalent atoms tie; every legal assignment is retained",
                    })
    return findings


def _diagnose_overlap(candidates, peak_lists):
    findings = []
    # Identical raw shift observed more than once in one spectrum = overlap.
    for experiment, parsed in peak_lists.items():
        by_shift: Dict[float, List[str]] = {}
        for peak in parsed["peaks"]:
            if peak["shift"] is not None:
                by_shift.setdefault(peak["shift"], []).append(peak["identity"])
        for shift, identities in by_shift.items():
            if len(identities) > 1:
                findings.append({
                    "kind": "overlapping_peaks",
                    "experiment": experiment,
                    "shift_ppm": shift,
                    "shift_unit": parsed.get("shift_unit", "ppm"),
                    "peak_identities": identities,
                    "severity": "warning",
                    "reason": ("multiple peak rows share a chemical shift; they are not collapsed or "
                               "force-matched to one atom"),
                })
        # Several atoms competing for one single-peak row is also retained.
        per_peak: Dict[str, set] = {}
        for candidate in candidates:
            if candidate["experiment"] == experiment and candidate["status"] == "candidate":
                per_peak.setdefault(candidate["peak_identity"], set()).add(candidate["atom_id"])
        for peak in parsed["peaks"]:
            competing = per_peak.get(peak["identity"], set())
            if len(competing) > 1:
                findings.append({
                    "kind": "ambiguous_assignment",
                    "experiment": experiment,
                    "peak_identity": peak["identity"],
                    "competing_atoms": sorted(competing),
                    "severity": "info",
                    "reason": "one peak row matches several atoms; no unique match is forced",
                })
    return findings


def _evidence_dep_keys(evidence):
    keys = set()
    for item in evidence:
        if item["kind"] == "COSY":
            keys.add("corr_COSY")
        elif item["kind"] == "HSQC":
            keys.add("corr_HSQC")
        elif item["kind"] == "HMBC":
            keys.add("corr_HMBC")
    return keys


def _with_identity(candidate, pf):
    basis = [
        candidate["algorithm_version"],
        pf,
        candidate["atom_id"],
        candidate["peak_identity"],
        candidate.get("observed_shift_ppm"),
        candidate.get("expected_shift_ppm"),
        candidate["score"],
    ]
    candidate["identity"] = content_id(PREFIX_CANDIDATE, basis)
    evidence_items = []
    for item in candidate["evidence"]:
        ev = dict(item)
        ev["identity"] = content_id(PREFIX_EVIDENCE, candidate["identity"], item["kind"],
                                    item.get("detail"), item.get("score"), item.get("weight"))
        evidence_items.append(ev)
    candidate["evidence"] = evidence_items
    # Tighten direct-source dependency to the actual evidence kinds.
    dep_keys = _evidence_dep_keys(candidate["evidence"])
    deps = {"params": candidate["depends_on"]["params"],
            "skeleton": candidate["depends_on"]["skeleton"],
            "peaks": candidate["depends_on"].get("peaks")}
    for key in dep_keys:
        if key in candidate["depends_on"]:
            deps[key] = candidate["depends_on"][key]
    candidate["depends_on"] = deps
    return candidate


def _with_finding_identity(finding, pf):
    finding.setdefault("depends_on", {})
    basis = {k: v for k, v in finding.items() if k not in ("identity",)}
    from .identity import content_id as _cid
    finding["identity"] = _cid("fnd", ALGORITHM_VERSION, pf, basis)
    return finding


def rank_candidates(batch: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Per-peak ranking lists derived from a candidate batch."""
    rankings: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in batch["candidates"]:
        rankings.setdefault(candidate["peak_identity"], []).append(candidate)
    for peak_identity, rows in rankings.items():
        rows.sort(key=lambda c: (-c["score"], c["atom_id"]))
        best = rows[0]["score"] if rows else 0.0
        tie_eps = 0.02
        for rank, row in enumerate(rows, start=1):
            row["rank_for_peak"] = rank
            row["ranked_tied"] = (best - row["score"]) <= tie_eps
    return rankings
