"""Assignment candidate engine.

Design rules (enforced by tests):

* Overlapped / missing peaks are never forced into a unique one-to-one match.
  Multiple sites may rank the same observed peak; multiple peaks may serve one
  site as alternatives.
* The coupling network (COSY/HSQC/HMBC) is *evidence only*: missing or extra
  edges never reject a candidate, they only change the network component.
* Candidates, rejection reasons, errors and unknown states are first-class.
* A systematic chemical-shift bias is detected from the residual distribution
  and reported; scores then carry raw and bias-corrected components.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import ALGORITHM_NAME, ALGORITHM_VERSION


def _gaussian_score(delta: float, tol: float) -> float:
    if delta <= tol:
        return 1.0
    return float(math.exp(-0.5 * ((delta - tol) / tol) ** 2))


def _collect_inputs(bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {
        "h1": [], "c13": [], "cosy": [], "hsqc": [], "hmbc": []
    }
    for src in bundle.get("sources", []):
        parsed = src.get("parsed") or {}
        kind = parsed.get("kind", src.get("kind"))
        recs = parsed.get("records", [])
        if kind == "h1_peaks":
            out["h1"] = recs
        elif kind == "c13_peaks":
            out["c13"] = recs
        elif kind == "cosy_edges":
            out["cosy"] = recs
        elif kind == "hsqc_edges":
            out["hsqc"] = recs
        elif kind == "hmbc_edges":
            out["hmbc"] = recs
    return out


def peak_id(rec: dict[str, Any]) -> str:
    return f"L{rec.get('line', 0)}@{rec.get('raw_shift_ppm', '')}"


def edge_id(rec: dict[str, Any]) -> str:
    return f"L{rec.get('line', 0)}@{rec.get('raw_a_ppm', '')}x{rec.get('raw_b_ppm', '')}"


def detect_overlaps(peaks: list[dict[str, Any]], tol_ppm: float = 0.015) -> list[dict[str, Any]]:
    """Observed peaks whose reported integral exceeds a single proton-site unit.

    These overlap signals stay shared between candidates; the engine never
    splits or uniquely allocates them.
    """
    overlaps = []
    for rec in peaks:
        integral = rec.get("integral_protons")
        if integral is None:
            continue
        if integral > 1.5:
            overlaps.append({
                "peak": peak_id(rec), "shift_ppm": rec.get("shift_ppm"),
                "integral_protons": integral,
                "reason": f"reported integral {integral:g} H spans more than one proton site",
                "resolution_tol_ppm": tol_ppm,
            })
    return overlaps


def _nearest_shift(target: float, peaks: list[dict[str, Any]], tol: float) -> list[dict[str, Any]]:
    hits = []
    for rec in peaks:
        shift = rec.get("shift_ppm")
        if shift is None:
            continue
        if abs(shift - target) <= tol:
            hits.append({"rec": rec, "delta": shift - target})
    hits.sort(key=lambda h: (abs(h["delta"]), h["rec"].get("line", 0)))
    return hits


def detect_global_offset(h_peaks: list[dict[str, Any]], sites: list[dict[str, Any]],
                         threshold_ppm: float) -> dict[str, Any]:
    """Fit a common additive delta ppm bias from nearest peak/site residuals.

    Returns bias=0 when no statistically coherent systematic shift is found.
    """
    residuals = []
    for site in sites:
        expected = site.get("expected_shift_ppm")
        if expected is None:
            continue
        best = None
        for rec in h_peaks:
            shift = rec.get("shift_ppm")
            if shift is None:
                continue
            d = shift - expected
            if best is None or abs(d) < abs(best):
                best = d
        if best is not None:
            residuals.append(best)
    if len(residuals) < 3:
        return {"bias_ppm": 0.0, "detected": False, "residuals_ppm": residuals,
                "threshold_ppm": threshold_ppm, "method": "median-nearest-residual"}
    arr = np.asarray(residuals, dtype=float)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    coherent = abs(med) >= threshold_ppm and mad < max(threshold_ppm / 2.0, 0.05)
    return {
        "bias_ppm": med if coherent else 0.0,
        "detected": bool(coherent),
        "median_residual_ppm": med,
        "mad_ppm": mad,
        "residuals_ppm": [float(x) for x in arr],
        "threshold_ppm": threshold_ppm,
        "method": "median-nearest-residual",
    }


def _obs_edge_matches_expected(obs_rec: dict[str, Any], a_target: float, b_target: float,
                               tol: float) -> bool:
    a = obs_rec.get("a_ppm")
    b = obs_rec.get("b_ppm")
    if a is None or b is None:
        return False
    direct = abs(a - a_target) <= tol and abs(b - b_target) <= tol
    swapped = abs(a - b_target) <= tol and abs(b - a_target) <= tol
    return direct or swapped


def network_evidence(kind: str, candidate_site: dict[str, Any], peak: dict[str, Any],
                     other_peak: dict[str, Any] | None, edges: list[dict[str, Any]],
                     skeleton: dict[str, Any], tol: float, bias: float) -> dict[str, Any]:
    """Fraction of *expected* edges involving this site that are observed.

    Extra/missing observed edges never reject; they are listed for review.
    Evidence coordinates are compared in the observed frame (expected + bias).
    """
    expected = skeleton.get("expected_correlations", {}).get(kind, [])
    site_id = candidate_site["id"]
    relevant = [e for e in expected if e.get("site") == site_id]
    atoms_by_id = {a["id"]: a for a in skeleton.get("atoms", [])}
    sites_by_id = {s["id"]: s for s in skeleton.get("proton_sites", [])}
    supported = []
    missing = []
    for exp in relevant:
        a_target = candidate_site.get("expected_shift_ppm", 0.0) + bias
        if kind == "COSY":
            other = sites_by_id.get(exp.get("other_site"))
            if other is None:
                continue
            b_target = other.get("expected_shift_ppm", 0.0) + bias
        else:
            atom = atoms_by_id.get(exp.get("atom"))
            if atom is None:
                continue
            b_target = atom.get("expected_shift_ppm", 0.0)
        found = next((e for e in edges if _obs_edge_matches_expected(e, a_target, b_target, tol)), None)
        if found:
            supported.append({"expected": exp, "evidence_edge": edge_id(found)})
        else:
            missing.append({"expected": exp})
    fraction = 1.0 if not relevant else len(supported) / len(relevant)
    return {
        "experiment": kind,
        "supported": supported,
        "missing_expected": missing,
        "support_ratio": round(fraction, 6),
        "role": "evidence-only",
    }


def _multiplicity_score(observed: str | None, expected: str | None) -> float:
    if observed is None or expected is None:
        return 0.0
    return 1.0 if observed == expected else 0.0


def _integral_component(rec: dict[str, Any], expected: float) -> tuple[float, bool]:
    """Overlap peaks stay feasible; exact match scores highest.

    Returns (score, is_overlap).  When the observed integral exceeds the
    site's expected integral the peak can still serve the site (the peak is
    shared), so the component is not forced to a unique winner.
    """
    integral = rec.get("integral_protons")
    if integral is None or expected is None or expected <= 0:
        return 0.5, False
    ratio = integral / expected
    if abs(ratio - 1.0) < 0.25:
        return 1.0, False
    if ratio > 1.25:
        return 0.55, True
    return max(0.0, ratio)


def build_proton_candidates(inputs: dict[str, Any], skeleton: dict[str, Any],
                            params: dict[str, Any], offset: dict[str, Any]) -> list[dict[str, Any]]:
    tol = float(params["shift_tolerance_h_ppm"])
    weights = {k: float(params[k]) for k in
               ("weight_shift", "weight_multiplicity", "weight_integral", "weight_network")}
    edge_tol = float(params["edge_match_tolerance_ppm"])
    reject_mult = float(params["reject_shift_multiples"])
    bias = float(offset.get("bias_ppm", 0.0))
    h_peaks = inputs["h1"]
    candidates: list[dict[str, Any]] = []
    overlap_ids = {o["peak"] for o in detect_overlaps(h_peaks)}

    for site in skeleton.get("proton_sites", []):
        expected = float(site["expected_shift_ppm"])
        feasible_hits = _nearest_shift(expected + bias, h_peaks, tol * reject_mult)
        if not feasible_hits:
            candidates.append({
                "candidate_id": f"CAND-H-{site['id']}-MISSING",
                "nucleus": "1H", "site_id": site["id"], "peak_id": None,
                "status": "unknown", "score": 0.0, "total_score": 0.0,
                "rejection_reasons": [f"no observed 1H peak within {tol * reject_mult:.2f} ppm of expected "
                                      f"{expected:g} ppm (missing peak; left unassigned)"],
                "components": {}, "evidence": [], "tie_with": [],
                "equivalence_group": site.get("equivalence_group"),
                "expected_shift_ppm": expected,
            })
            continue
        for hit in feasible_hits:
            rec = hit["rec"]
            raw_delta = hit["delta"] - bias
            corrected_delta = hit["delta"] - bias
            shift_raw = _gaussian_score(raw_delta - bias, tol)
            shift_corrected = _gaussian_score(corrected_delta, tol)
            mult_s = _multiplicity_score(rec.get("multiplicity"), site.get("expected_multiplicity"))
            int_s, is_overlap = _integral_component(rec, float(site.get("expected_integral", 1.0)))
            evid = [
                network_evidence("COSY", site, rec, None, inputs["cosy"], skeleton, edge_tol, bias),
                network_evidence("HSQC", site, rec, None, inputs["hsqc"], skeleton, edge_tol, bias),
                network_evidence("HMBC", site, rec, None, inputs["hmbc"], skeleton, edge_tol, bias),
            ]
            net_s = float(np.mean([e["support_ratio"] for e in evid]))
            total = (weights["weight_shift"] * shift_corrected
                     + weights["weight_multiplicity"] * mult_s
                     + weights["weight_integral"] * int_s
                     + weights["weight_network"] * net_s)
            reasons = []
            status = "candidate"
            if abs(raw_delta - bias) > tol * reject_mult:
                reasons.append(f"shift residual {raw_delta - bias:+.3f} ppm beyond reject bound "
                               f"{tol * reject_mult:.2f}")
            if shift_corrected < 0.2:
                reasons.append(f"shift component weak ({shift_corrected:.2f})")
            if reasons:
                status = "rejected"
            cand = {
                "candidate_id": f"CAND-H-{site['id']}-{peak_id(rec)}",
                "nucleus": "1H", "site_id": site["id"], "peak_id": peak_id(rec),
                "observed_shift_ppm": rec.get("shift_ppm"),
                "raw_observed_shift": rec.get("raw_shift_ppm"),
                "expected_shift_ppm": expected,
                "raw_delta_ppm": round(raw_delta, 6),
                "corrected_delta_ppm": round(corrected_delta, 6),
                "status": status, "score": round(total, 6), "total_score": round(total, 6),
                "components": {
                    "shift_raw": round(shift_raw, 6),
                    "shift_corrected": round(shift_corrected, 6),
                    "multiplicity": round(mult_s, 6),
                    "integral": round(int_s, 6),
                    "network": round(net_s, 6),
                },
                "evidence": evid,
                "rejection_reasons": reasons,
                "ambiguity_flags": ["overlap-peak-shared"] if peak_id(rec) in overlap_ids or is_overlap else [],
                "tie_with": [],
                "equivalence_group": site.get("equivalence_group"),
                "sources": [{"kind": "h1_peaks", "record_line": rec.get("line")}],
            }
            candidates.append(cand)
    _annotate_ties(candidates, h_peaks, float(params["tie_epsilon_ppm"]))
    return candidates


def build_carbon_candidates(inputs: dict[str, Any], skeleton: dict[str, Any],
                            params: dict[str, Any]) -> list[dict[str, Any]]:
    tol = float(params["shift_tolerance_c_ppm"])
    reject_mult = float(params["reject_shift_multiples"])
    c_peaks = inputs["c13"]
    candidates: list[dict[str, Any]] = []
    carbon_atoms = [a for a in skeleton.get("atoms", []) if a.get("element") == "C"
                    and a.get("expected_shift_ppm") is not None]
    for atom in carbon_atoms:
        expected = float(a["expected_shift_ppm"])
        hits = _nearest_shift(expected, c_peaks, tol * reject_mult)
        if not hits:
            candidates.append({
                "candidate_id": f"CAND-C-{atom['id']}-MISSING",
                "nucleus": "13C", "site_id": atom["id"], "peak_id": None,
                "status": "unknown", "score": 0.0, "total_score": 0.0,
                "rejection_reasons": [f"no observed 13C peak within {tol * reject_mult:.2f} ppm of expected "
                                      f"{expected:g} ppm"],
                "components": {}, "evidence": [], "tie_with": [],
                "equivalence_group": atom.get("equivalence_group"),
                "expected_shift_ppm": expected,
            })
            continue
        for hit in hits:
            rec = hit["rec"]
            delta = hit["delta"]
            shift_s = _gaussian_score(delta, tol)
            reasons = []
            status = "candidate"
            if abs(delta) > tol * reject_mult or shift_s < 0.2:
                reasons.append(f"shift residual {delta:+.3f} ppm beyond reject bound {tol * reject_mult:.2f}")
                status = "rejected"
            candidates.append({
                "candidate_id": f"CAND-C-{atom['id']}-{peak_id(rec)}",
                "nucleus": "13C", "site_id": atom["id"], "peak_id": peak_id(rec),
                "observed_shift_ppm": rec.get("shift_ppm"),
                "raw_observed_shift": rec.get("raw_shift_ppm"),
                "expected_shift_ppm": expected,
                "raw_delta_ppm": round(delta, 6),
                "corrected_delta_ppm": round(delta, 6),
                "status": status, "score": round(shift_s, 6), "total_score": round(shift_s, 6),
                "components": {"shift": round(shift_s, 6)},
                "evidence": [], "rejection_reasons": reasons,
                "ambiguity_flags": [], "tie_with": [],
                "equivalence_group": atom.get("equivalence_group"),
                "sources": [{"kind": "c13_peaks", "record_line": rec.get("line")}],
            })
    _annotate_ties(candidates, c_peaks, float(params.get("tie_epsilon_ppm", 0.001)))
    return candidates


def _annotate_ties(candidates: list[dict[str, Any]], peaks: list[dict[str, Any]], epsilon: float) -> None:
    """Two kinds of non-unique rank are retained as data:

    * different sites/atoms share one observed peak (overlap or symmetry);
    * within one site, two peaks score equally (ties).
    Equivalent nuclei in a symmetry group legitimately converge on the same
    peak and are recorded with a ``symmetry`` flag, never broken arbitrarily.
    """
    peak_to_sites: dict[str, set[str]] = {}
    for cand in candidates:
        if cand.get("peak_id") and cand["status"] == "candidate":
            peak_to_sites.setdefault(cand["peak_id"], set()).add(cand["site_id"])
    for cand in candidates:
        pid = cand.get("peak_id")
        if pid and len(peak_to_sites.get(pid, ())) > 1:
            flag = "symmetry-shared-peak" if cand.get("equivalence_group") else "overlap-shared-peak"
            cand.setdefault("ambiguity_flags", []).append(flag)
        if not pid:
            continue
        same_site = [c for c in candidates if c["site_id"] == cand["site_id"]
                     and c["status"] == "candidate"]
        best = max((c["total_score"] for c in same_site), default=0.0)
        if cand["status"] == "candidate" and abs(cand["total_score"] - best) <= epsilon:
            ties = [c["candidate_id"] for c in same_site
                    if c is not cand and abs(c["total_score"] - best) <= epsilon]
            if ties and not cand["tie_with"]:
                cand["tie_with"] = ties
    for cands in (build_symmetric_ties,):
        cands(candidates)


def build_symmetric_ties(candidates: list[dict[str, Any]]) -> None:
    by_peak: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        if cand.get("peak_id") and cand.get("equivalence_group") and cand["status"] == "candidate":
            by_peak.setdefault(cand["peak_id"], []).append(cand)
    for pid, group in by_peak.items():
        groups = {c["equivalence_group"] for c in group}
        if len(group) > 1:
            ids = [c["candidate_id"] for c in group]
            for c in group:
                c["tie_with"] = sorted(set(c["tie_with"]) | set(ids) - {c["candidate_id"]})
                c.setdefault("ambiguity_flags", []).append("symmetric-equivalent-nuclei")


def parameter_fingerprint(params: dict[str, Any]) -> str:
    import hashlib
    import json

    payload = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _unassigned_peaks(inputs: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used_h = {c.get("peak_id") for c in candidates if c["nucleus"] == "1H"}
    used_c = {c.get("peak_id") for c in candidates if c["nucleus"] == "13C"}
    unknown = []
    for rec in inputs["h1"]:
        pid = peak_id(rec)
        if pid not in used_h:
            unknown.append({"nucleus": "1H", "peak_id": pid,
                            "shift_ppm": rec.get("shift_ppm"),
                            "status": "unknown",
                            "reason": "peak matches no skeleton site candidate within reject bound"})
    for rec in inputs["c13"]:
        pid = peak_id(rec)
        if pid not in used_c:
            unknown.append({"nucleus": "13C", "peak_id": pid,
                            "shift_ppm": rec.get("shift_ppm"),
                            "status": "unknown",
                            "reason": "peak matches no skeleton atom candidate within reject bound"})
    return unknown


def derive_candidates(bundle: dict[str, Any]) -> dict[str, Any]:
    """Run one pure derivation.

    ``bundle`` maps source kinds to *current* versions:
    {kind: {"source_id", "version", "parsed", "raw_text"}} plus params/context.
    """
    skeleton_src = bundle["skeleton"]
    skeleton = skeleton_src["parsed"]["metadata"]
    params = dict(bundle["params"]["parsed"]["metadata"])
    context = bundle["context"]["parsed"]["metadata"]
    inputs = _collect_inputs({"sources": [{"kind": k, "parsed": v["parsed"]}
                                          for k, v in bundle.items() if isinstance(v, dict)]})
    offset = detect_global_offset(inputs["h1"], skeleton.get("proton_sites", []),
                                  float(params["global_offset_detect_threshold_ppm"]))
    proton = build_proton_candidates(inputs, skeleton, params, offset)
    carbon = build_carbon_candidates(inputs, skeleton, params)
    candidates = proton + carbon
    overlaps = detect_overlaps(inputs["h1"])
    unresolved_sites = []
    for site in skeleton.get("proton_sites", []):
        live = [c for c in proton if c["site_id"] == site["id"] and c["status"] == "candidate"]
        if len({c["peak_id"] for c in live}) != 1 or any(c["tie_with"] for c in live) \
                or any("overlap" in f for c in live for f in c.get("ambiguity_flags", [])):
            unresolved_sites.append({
                "site_id": site["id"],
                "reason": "not uniquely decidable: all legal candidates retained",
                "candidate_ids": [c["candidate_id"] for c in live],
            })
    source_versions = {k: {"source_id": v["source_id"], "version": v["version"]}
                       for k, v in bundle.items() if isinstance(v, dict)}
    return {
        "algorithm": ALGORITHM_NAME,
        "algorithm_version": ALGORITHM_VERSION,
        "parameter_fingerprint": parameter_fingerprint(params),
        "parameters": params,
        "context_excerpt": {k: context.get(k) for k in
                            ("solvent", "temperature_k", "reference_standard",
                             "spectrometer_frequency_h1_mhz", "spectrometer_frequency_c13_mhz")},
        "source_versions": source_versions,
        "global_offset": offset,
        "overlapped_peaks": overlaps,
        "candidates": candidates,
        "unknown_peaks": _unassigned_peaks(inputs, candidates),
        "unresolved_sites": unresolved_sites,
        "rankings": _rankings(candidates),
    }


def _rankings(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    rankings: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        key = f"{cand['nucleus']}:{cand['site_id']}"
        rankings.setdefault(key, []).append({
            "candidate_id": cand["candidate_id"],
            "peak_id": cand.get("peak_id"),
            "score": cand["total_score"],
            "status": cand["status"],
            "tie_with": cand.get("tie_with", []),
            "ambiguity_flags": cand.get("ambiguity_flags", []),
        })
    for rows in rankings.values():
        rows.sort(key=lambda r: (-r["score"], r.get("peak_id") or ""))
    return rankings
