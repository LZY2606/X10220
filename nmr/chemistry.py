"""Assignment-candidate engine.

The engine never produces a single forced answer. For every nucleus site it
emits all legal peak candidates with scores, rejected peaks with reasons and
an explicit status (overlap / symmetric / tie / evidence-incomplete).
Correlation networks are evidence only: a missing edge never removes a
candidate, it is reported as missing evidence.
"""
from __future__ import annotations

from collections import deque

import numpy as np

from .ident import ALGORITHM_VERSION, fingerprint, stable_id

DEFAULT_PARAMS = {
    "tolerance_ppm": {"H": 0.05, "C": 0.6},
    "shift_sigma_ppm": {"H": 0.03, "C": 0.4},
    "weights": {"shift": 0.6, "hsqc": 0.15, "hmbc": 0.1, "cosy": 0.1,
                "multiplicity": 0.03, "integral": 0.02},
    "missing_edge_penalty": 0.05,
    "edge_snap_ppm": {"H": 0.04, "C": 0.6},
    "bootstrap_samples": 40,
    "seed": 20260928,
}

SIGMA_FALLBACK = {"H": 0.03, "C": 0.4}


def params_fingerprint(params: dict) -> str:
    return fingerprint(params, length=12)


def _usable_peaks(peaks: list[dict]) -> list[dict]:
    return [p for p in peaks if p["shift"]["value"] is not None]


def _nucleus_kind(nucleus: str) -> str:
    return "H" if nucleus in ("H", "1H") else "C"


def estimate_bias(peaks_by_nucleus: dict, skeleton: dict, params: dict) -> dict:
    """Estimate a single global shift offset per nucleus (median residuals).

    The estimate is derived only from pairs inside tolerance, which makes the
    method fail-safe when no alignment exists.
    """
    result = {}
    tol = params["tolerance_ppm"]
    for nucleus, peaks in peaks_by_nucleus.items():
        kind = _nucleus_kind(nucleus)
        usable = _usable_peaks(peaks)
        sites = [s for s in skeleton["sites"] if s["kind"] == kind
                 and s["expected_shift"]["value"] is not None]
        residuals = []
        for peak in usable:
            nearest = min(sites, key=lambda s: abs(
                peak["shift"]["value"] - s["expected_shift"]["value"]), default=None)
            if nearest is not None and abs(
                    peak["shift"]["value"] - nearest["expected_shift"]["value"]) <= tol[kind]:
                residuals.append(peak["shift"]["value"] - nearest["expected_shift"]["value"])
        if residuals:
            bias = float(np.median(np.array(residuals, dtype=float)))
        else:
            bias = 0.0
        result[kind] = {
            "bias_ppm": round(bias, 6),
            "paired_residuals": [round(float(r), 6) for r in residuals],
            "applied": len(residuals) > 0,
        }
    return result


def _bond_map(skeleton: dict) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {s["id"]: set() for s in skeleton["sites"]}
    for a, b in skeleton["bonds"]:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
    return graph


def _shortest_paths(graph: dict[str, set[str]], start: str, max_len: int = 3) -> dict[str, int]:
    distances = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if distances[node] >= max_len:
            continue
        for nxt in graph.get(node, ()):
            if nxt not in distances:
                distances[nxt] = distances[node] + 1
                queue.append(nxt)
    return distances


def snap_edges(edges_by_type: dict, peaks_by_nucleus: dict, params: dict) -> dict:
    """Attach raw 2D correlation coordinates to 1D peaks; failures are diag."""
    snap_tol = params["edge_snap_ppm"]
    snapped = {"COSY": [], "HSQC": [], "HMBC": []}
    diagnostics: list[dict] = []

    def nearest(coord: dict, peaks: list[dict], kind: str):
        usable = _usable_peaks(peaks)
        if coord["value"] is None or not usable:
            return None
        best = min(usable, key=lambda p: abs(p["shift"]["value"] - coord["value"]))
        if abs(best["shift"]["value"] - coord["value"]) <= snap_tol[kind]:
            return best
        return None

    for edge in edges_by_type.get("COSY", []):
        peaks = peaks_by_nucleus.get("1H", [])
        a = nearest(edge["coordinates"][0], peaks, "H")
        b = nearest(edge["coordinates"][1], peaks, "H")
        record = {**edge, "peak_a": a["stable_id"] if a else None,
                  "peak_b": b["stable_id"] if b else None}
        if a is None or b is None:
            diagnostics.append({"edge": edge["stable_id"], "type": "COSY",
                                "reason": "coordinate could not be snapped to a 1D peak"})
        snapped["COSY"].append(record)
    for etype in ("HSQC", "HMBC"):
        for edge in edges_by_type.get(etype, []):
            h_peak = nearest(edge["coordinates"][0], peaks_by_nucleus.get("1H", []), "H")
            c_peak = nearest(edge["coordinates"][1], peaks_by_nucleus.get("13C", []), "C")
            record = {**edge, "peak_a": h_peak["stable_id"] if h_peak else None,
                      "peak_b": c_peak["stable_id"] if c_peak else None}
            if h_peak is None or c_peak is None:
                diagnostics.append({"edge": edge["stable_id"], "type": etype,
                                    "reason": "coordinate could not be snapped to a 1D peak"})
            snapped[etype].append(record)
    return {"edges": snapped, "diagnostics": diagnostics}


def _symmetry_peers(site: dict, skeleton: dict) -> list[str]:
    group = site.get("symmetry")
    if not group:
        return []
    return sorted(s["id"] for s in skeleton["sites"]
                  if s.get("symmetry") == group and s["id"] != site["id"])


def _observed_edge_set(snapped: dict) -> dict:
    out = {}
    for etype, records in snapped["edges"].items():
        pairs = set()
        for record in records:
            if record.get("peak_a") and record.get("peak_b"):
                pairs.add((record["peak_a"], record["peak_b"]))
                pairs.add((record["peak_b"], record["peak_a"]))
        out[etype] = pairs
    return out


def _candidate_site_distance(site: dict, skeleton: dict) -> dict[str, int]:
    graph = _bond_map(skeleton)
    return _shortest_paths(graph, site["id"])


def _legal_peaks(site, peaks, bias_info, params):
    """Return (legal, rejected) peaks for a site with explicit reject reasons."""
    kind = site["kind"]
    bias = bias_info[kind]["bias_ppm"]
    tol = params["tolerance_ppm"][kind]
    legal, rejected = [], []
    for peak in peaks:
        value = peak["shift"]["value"]
        if value is None:
            rejected.append({"peak_stable_id": peak["stable_id"], "peak_id": peak["id"],
                             "reason": "shift value unconvertible (kept raw in source)"})
            continue
        residual = (value - bias) - site["expected_shift"]["value"]
        if abs(residual) <= tol:
            legal.append((peak, residual))
        else:
            rejected.append({"peak_stable_id": peak["stable_id"], "peak_id": peak["id"],
                             "reason": f"out of tolerance: residual {round(residual, 4)} ppm",
                             "residual_ppm": round(residual, 6)})
    return legal, rejected


def _site_expected_edges(site, skeleton):
    """Expected HSQC/COSY/HMBC relations for a site, purely from topology."""
    distances = _shortest_paths(_bond_map(skeleton), site["id"])
    expected = {"HSQC": [], "COSY": [], "HMBC": []}
    for other_id, dist in distances.items():
        if other_id == site["id"]:
            continue
        other = next(s for s in skeleton["sites"] if s["id"] == other_id)
        if site["kind"] == "H" and other["kind"] == "C" and site.get("attached") == other_id:
            expected["HSQC"].append(other_id)
        if site["kind"] == "C" and other["kind"] == "H" and other.get("attached") == site["id"]:
            expected["HSQC"].append(other_id)
        if site["kind"] == "H" and other["kind"] == "H" and dist == 2:
            expected["COSY"].append(other_id)
        if dist in (2, 3):
            if (site["kind"] == "H" and other["kind"] == "C") or \
               (site["kind"] == "C" and other["kind"] == "H"):
                expected["HMBC"].append({"site": other_id, "distance": dist})
    return expected


def _score_candidate(site, peak, residual, skeleton, snapped_edges, bias_info, params,
                     legal_by_site):
    kind = site["kind"]
    sigma = params["shift_sigma_ppm"][kind]
    weights = params["weights"]
    components: dict[str, float] = {}
    matched, missing = [], []

    shift_score = float(np.exp(-0.5 * (residual / sigma) ** 2))
    components["shift"] = round(weights["shift"] * shift_score, 9)

    mult_value = site["multiplicity"]["value"]
    if mult_value:
        same = peak["multiplicity"]["value"] == mult_value
        components["multiplicity"] = round(weights["multiplicity"] * (1.0 if same else 0.0), 9)

    if kind == "H" and peak["integral"]["value"] is not None:
        expected_integral = 1.0 + len(_symmetry_peers(site, skeleton))
        fit = 1.0 if abs(peak["integral"]["value"] - expected_integral) <= 0.15 else 0.0
        components["integral"] = round(weights["integral"] * fit, 9)

    obs = _observed_edge_set(snapped_edges)
    expected = _site_expected_edges(site, skeleton)

    # HSQC: site is a proton, partner is its host carbon.
    if kind == "H" and site.get("attached"):
        host = next((s for s in skeleton["sites"] if s["id"] == site["attached"]), None)
        host_peak_ids = [pk["stable_id"] for pk, _r in legal_by_site.get(host["id"], [])]
        if host_peak_ids:
            hit = any((peak["stable_id"], hp) in obs["HSQC"] for hp in host_peak_ids)
            components["hsqc"] = round(weights["hsqc"] * (1.0 if hit else 0.0), 9)
            (matched if hit else missing).append({"type": "HSQC", "with_site": site["attached"]})

    # COSY: fraction of expected proton neighbours with a legal peak that is
    # observed coupled to this candidate peak (network as evidence only).
    if kind == "H":
        supported, total = 0, 0
        for other_id in expected["COSY"]:
            other_peak_ids = [pk["stable_id"] for pk, _r in legal_by_site.get(other_id, [])]
            for other_pid in other_peak_ids:
                total += 1
                if (peak["stable_id"], other_pid) in obs["COSY"]:
                    supported += 1
                    matched.append({"type": "COSY", "with_site": other_id,
                                    "with_peak": other_pid})
        if total:
            components["cosy"] = round(weights["cosy"] * supported / total, 9)

    # HMBC: fraction of expected long-range partners reached via observed edges.
    supported, total, absent = 0, 0, 0
    for partner in expected["HMBC"]:
        other_id = partner["site"]
        other_peak_ids = [pk["stable_id"] for pk, _r in legal_by_site.get(other_id, [])]
        if not other_peak_ids:
            continue
        for other_pid in other_peak_ids:
            total += 1
            if (peak["stable_id"], other_pid) in obs["HMBC"]:
                supported += 1
                matched.append({"type": "HMBC", "with_site": other_id,
                                "distance": partner["distance"], "with_peak": other_pid})
            else:
                absent += 1
    if total:
        components["hmbc"] = round(weights["hmbc"] * supported / total, 9)
        if absent:
            missing.append({"type": "HMBC", "missing_count": absent, "expected_count": total})
            components["hmbc"] = round(
                max(0.0, components["hmbc"] - params["missing_edge_penalty"]
                    * absent / total), 9)

    total_score = round(sum(components.values()), 9)
    return {
        "peak_id": peak["id"],
        "peak_stable_id": peak["stable_id"],
        "residual_ppm": round(residual, 6),
        "score_components": components,
        "matched_edges": matched,
        "missing_edges": missing,
        "total_score": total_score,
    }


def _candidate_key(candidate: dict) -> tuple:
    return (-candidate["total_score"], candidate["peak_stable_id"])


def _resample_snapped(snapped: dict, rng: np.random.Generator) -> dict:
    resampled = {"edges": {}, "diagnostics": snapped["diagnostics"]}
    for etype, records in snapped["edges"].items():
        if not records:
            resampled["edges"][etype] = records
            continue
        idx = rng.integers(0, len(records), size=len(records))
        resampled["edges"][etype] = [records[i] for i in idx]
    return resampled


def _bootstrap_stability(site, scored, skeleton, snapped, bias_info, params,
                         legal_by_site) -> dict:
    sample_count = params["bootstrap_samples"]
    if sample_count <= 0:
        return {c["peak_stable_id"]: 1.0 for c in scored}
    rng = np.random.default_rng(params["seed"])
    stability = {c["peak_stable_id"]: 0 for c in scored}
    if not scored:
        return stability
    has_edges = any(snapped["edges"][e] for e in snapped["edges"])
    if not has_edges:
        for c in scored:
            stability[c["peak_stable_id"]] = 1.0
        return stability
    peaks_by_id = {c["peak_stable_id"]: c for c in scored}
    peaks_by_id_full = {}
    for peak_list in legal_by_site.values():
        for peak, _res in peak_list:
            peaks_by_id_full[peak["stable_id"]] = peak
    for _ in range(sample_count):
        sampled = _resample_snapped(snapped, rng)
        rescores = {}
        for cand in scored:
            peak = peaks_by_id_full[cand["peak_stable_id"]]
            rc = _score_candidate(site, peak, cand["residual_ppm"], skeleton, sampled,
                                  bias_info, params, legal_by_site)
            rescores[cand["peak_stable_id"]] = rc["total_score"]
        best = max(rescores.values())
        winners = [pid for pid, value in rescores.items() if abs(value - best) < 1e-9]
        if len(winners) == 1:
            stability[winners[0]] += 1
    return {pid: round(count / sample_count, 4) for pid, count in stability.items()}


def run_candidates(skeleton: dict, parsed_sources: dict, params: dict, *,
                   run_index: int, basis_versions: dict) -> dict:
    """Build every legal/ambiguous/rejected assignment candidate per site.

    ``parsed_sources`` maps nucleus ("1H"/"13C") -> parsed peak-list version.
    """
    peaks_by_nucleus = {nuc: parsed_sources[nuc]["peaks"] for nuc in parsed_sources}
    edges_by_type = {"COSY": [], "HSQC": [], "HMBC": []}
    conditions_by_nucleus = {}
    for nuc, parsed in parsed_sources.items():
        for etype in edges_by_type:
            edges_by_type[etype].extend(parsed["edges"].get(etype, []))
        conditions_by_nucleus[nuc] = parsed["conditions"]

    bias_info = estimate_bias(peaks_by_nucleus, skeleton, params)
    snapped = snap_edges(edges_by_type, peaks_by_nucleus, params)

    legal_by_site: dict[str, list] = {}
    rejected_by_site: dict[str, list] = {}
    for site in skeleton["sites"]:
        nuc = "1H" if site["kind"] == "H" else "13C"
        peaks = peaks_by_nucleus.get(nuc, [])
        legal, rejected = _legal_peaks(site, peaks, bias_info, params)
        legal_by_site[site["id"]] = legal
        rejected_by_site[site["id"]] = rejected

    site_results = []
    for site in skeleton["sites"]:
        legal = legal_by_site[site["id"]]
        scored = [
            _score_candidate(site, peak, residual, skeleton, snapped, bias_info,
                             params, legal_by_site)
            for peak, residual in legal
        ]
        scored.sort(key=_candidate_key)
        stability = _bootstrap_stability(site, scored, skeleton, snapped, bias_info,
                                         params, legal_by_site)
        for candidate in scored:
            candidate["bootstrap_stability"] = stability[candidate["peak_stable_id"]]

        flags = []
        if site.get("symmetry") and _symmetry_peers(site, skeleton):
            flags.append("symmetric")
        if not scored:
            status = "no_legal_peak"
            flags.append("evidence-incomplete")
        else:
            tie = len(scored) > 1 and abs(
                scored[0]["total_score"] - scored[1]["total_score"]) < 1e-9
            if tie:
                flags.append("tie")
            top_pid = scored[0]["peak_stable_id"]
            sharing = {other_id for other_id, other_legal in legal_by_site.items()
                       if other_id != site["id"]
                       and any(pk["stable_id"] == top_pid for pk, _r in other_legal)
                       and not (_same_symmetry(site, other_id, skeleton))}
            if sharing:
                flags.append("ambiguous-overlap")
            if any(candidate["missing_edges"] for candidate in scored):
                flags.append("evidence-incomplete")
            status = "ambiguous" if "tie" in flags or "ambiguous-overlap" in flags \
                else "ranked"
        site_results.append({
            "site_id": site["id"],
            "site_stable_id": site["stable_id"],
            "nucleus": site["kind"],
            "status": status,
            "flags": flags,
            "symmetry_peers": _symmetry_peers(site, skeleton),
            "candidates": scored,
            "rejected": rejected_by_site[site["id"]],
        })

    run_id = stable_id("run", skeleton["compound"], params_fingerprint(params),
                       {nuc: basis_versions.get(nuc) for nuc in sorted(basis_versions)},
                       run_index)
    return {
        "run_id": run_id,
        "run_index": run_index,
        "algorithm_version": ALGORITHM_VERSION,
        "params": params,
        "params_fingerprint": params_fingerprint(params),
        "basis_versions": basis_versions,
        "conditions": conditions_by_nucleus,
        "bias": bias_info,
        "snapped_edges": snapped["edges"],
        "edge_diagnostics": snapped["diagnostics"],
        "site_results": site_results,
    }


def _same_symmetry(site_a, other_id: str, skeleton: dict) -> bool:
    other = next((s for s in skeleton["sites"] if s["id"] == other_id), None)
    group_a = site_a.get("symmetry")
    return bool(group_a and other and other.get("symmetry") == group_a)


def affected_sites(previous_run: dict | None, new_run: dict) -> list[str]:
    """Sites whose candidates/evidence actually differ between two runs."""
    if previous_run is None:
        return sorted(r["site_id"] for r in new_run["site_results"])
    old = {r["site_id"]: r for r in previous_run["site_results"]}
    affected = []
    for result in new_run["site_results"]:
        prior = old.get(result["site_id"])
        if prior is None or _comparable(result) != _comparable(prior):
            affected.append(result["site_id"])
    return sorted(affected)


def _comparable(result: dict) -> list:
    return [(c["peak_stable_id"], c["total_score"], c["residual_ppm"])
            for c in result["candidates"]]
