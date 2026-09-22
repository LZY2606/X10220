"""Domain computation: assignment candidates from peaks, skeleton and
correlation edges.

Rules honoured here (mirrored by machine-readable judgments in the case pack):

* overlapped peaks and missing peaks are never forced into a unique match;
  every legal candidate is kept as first-class data with flags;
* the coupling network (COSY/HSQC/HMBC) is used as *evidence* only - a missing
  edge lowers the score and raises a flag, it never invalidates a candidate;
* every derived value carries the algorithm version, a parameter fingerprint
  and its direct sources, so a local input change expires exactly the results
  that depend on it.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

ALGO_VERSION = "1.0.0"


def fingerprint(obj):
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def overlap_groups(peaks, tol_ppm):
    """Group 1D peaks whose shifts are closer than ``tol_ppm``."""
    pts = sorted(
        (p for p in peaks if p.get("shift_ppm") is not None),
        key=lambda p: p["shift_ppm"],
    )
    groups, current = [], []
    for p in pts:
        if current and abs(p["shift_ppm"] - current[-1]["shift_ppm"]) > tol_ppm:
            groups.append(current)
            current = []
        current.append(p)
    if current:
        groups.append(current)
    out = {}
    for i, g in enumerate(groups, 1):
        if len(g) > 1:
            gid = "OV%d" % i
            for p in g:
                out[p["id"]] = gid
    return out


def _best_peak(predicted, peaks):
    best, best_d = None, None
    for p in peaks:
        if p.get("shift_ppm") is None:
            continue
        d = abs(p["shift_ppm"] - predicted)
        if best_d is None or d < best_d:
            best, best_d = p, d
    return best


def _find_cosy(edges, peak_a, peak_b):
    for e in edges:
        ids = [ep.get("peak") for ep in e["endpoints"]]
        if {peak_a, peak_b} == set(ids):
            return e
    return None


def _find_edge_by_shifts(edges, c_shift, h_shift, tol_c, tol_h):
    for e in edges:
        c_end = [ep for ep in e["endpoints"] if ep.get("nucleus") == "13C"]
        h_end = [ep for ep in e["endpoints"] if ep.get("nucleus") == "1H"]
        if not c_end or not h_end:
            continue
        ce, he = c_end[0], h_end[0]
        if ce.get("shift_ppm") is None or he.get("shift_ppm") is None:
            continue
        if abs(ce["shift_ppm"] - c_shift) <= tol_c and abs(he["shift_ppm"] - h_shift) <= tol_h:
            return e
    return None


def compute_candidates(docs, versions, params):
    """Deterministic candidate generation.

    ``docs`` maps doc_id -> normalized document; ``versions`` maps
    doc_id -> current version int.  Returns a list of candidate dicts.
    """
    skel = docs.get("skeleton", {})
    p1h = docs.get("peaks_1h", {}).get("peaks", [])
    p13c = docs.get("peaks_13c", {}).get("peaks", [])
    edges = {
        "cosy": docs.get("edges_cosy", {}).get("edges", []),
        "hsqc": docs.get("edges_hsqc", {}).get("edges", []),
        "hmbc": docs.get("edges_hmbc", {}).get("edges", []),
    }
    tol_h = float(params.get("shift_tol_1h_ppm", 0.15))
    tol_c = float(params.get("shift_tol_13c_ppm", 1.0))
    ov_tol = float(params.get("overlap_tol_ppm", 0.03))
    w_shift = float(params.get("w_shift", 0.6))
    w_coupling = float(params.get("w_coupling", 0.4))
    tie_eps = float(params.get("tie_epsilon", 1e-6))

    ov = overlap_groups(p1h, ov_tol)
    symmetry = {}
    for group in skel.get("symmetry_groups", []):
        for a in group:
            symmetry[a] = [x for x in group if x != a]

    param_fp = fingerprint(params)
    atoms = sorted(skel.get("atoms", []), key=lambda a: a["id"])
    predicted = {a["id"]: a.get("predicted_shift_ppm") for a in atoms}
    expected = skel.get("expected_correlations", [])

    candidates = []
    for atom in atoms:
        nucleus = {"H": "1H", "C": "13C"}.get(atom.get("element"))
        if nucleus is None:
            continue
        peaks = p1h if nucleus == "1H" else p13c
        tol = tol_h if nucleus == "1H" else tol_c
        if nucleus == "1H":
            src_docs = ["peaks_1h", "edges_cosy", "skeleton", "conditions", "params"]
        else:
            src_docs = ["peaks_13c", "edges_hsqc", "edges_hmbc", "skeleton", "conditions", "params"]
        sources = ["%s@%d" % (d, versions[d]) for d in src_docs if d in versions]
        src_fp = fingerprint(sources)
        atom_expected = [c for c in expected if atom["id"] in c.get("atoms", [])]

        atom_cands = []
        for peak in sorted(peaks, key=lambda p: p["id"]):
            ps = peak.get("shift_ppm")
            ap = predicted.get(atom["id"])
            if ps is None or ap is None:
                continue
            delta = abs(ps - ap)
            shift_score = float(np.exp(-delta / tol))
            if shift_score < 0.05:
                continue
            supporting, missing, unknown = [], [], []
            for corr in atom_expected:
                partner = [x for x in corr["atoms"] if x != atom["id"]][0]
                ctype = corr["type"]
                if ctype == "cosy" and nucleus == "1H":
                    q = _best_peak(predicted.get(partner), p1h) \
                        if predicted.get(partner) is not None else None
                    if q is None:
                        unknown.append({"type": "cosy", "partner": partner,
                                        "reason": "partner peak unknown"})
                        continue
                    edge = _find_cosy(edges["cosy"], peak["id"], q["id"])
                    item = {"type": "cosy", "partner": partner, "partner_peak": q["id"]}
                    if edge:
                        item["edge"] = edge["id"]
                        supporting.append(item)
                    else:
                        missing.append(item)
                elif ctype in ("hsqc", "hmbc") and nucleus == "13C":
                    hpred = predicted.get(partner)
                    if hpred is None:
                        unknown.append({"type": ctype, "partner": partner,
                                        "reason": "partner shift unknown"})
                        continue
                    edge = _find_edge_by_shifts(edges[ctype], ps, hpred, tol_c, tol_h)
                    item = {"type": ctype, "partner": partner}
                    if edge:
                        item["edge"] = edge["id"]
                        supporting.append(item)
                    else:
                        missing.append(item)
            denom = len(supporting) + len(missing)
            coupling_score = len(supporting) / denom if denom else 0.5
            score = round(w_shift * shift_score + w_coupling * coupling_score, 6)
            flags = []
            if peak["id"] in ov:
                flags.append("overlap")
            if missing:
                flags.append("missing_evidence")
            if atom["id"] in symmetry:
                flags.append("symmetric")
            cid = "cand:%s:%s:%s:%s:%s" % (
                atom["id"], peak["id"], ALGO_VERSION, param_fp, src_fp)
            atom_cands.append({
                "candidate_id": cid,
                "atom_id": atom["id"],
                "peak_id": peak["id"],
                "nucleus": nucleus,
                "score": score,
                "shift_evidence": {
                    "predicted_ppm": ap,
                    "observed_ppm": ps,
                    "delta_ppm": round(delta, 6),
                    "tolerance_ppm": tol,
                    "score": round(shift_score, 6),
                },
                "coupling_evidence": {
                    "supporting": supporting,
                    "missing": missing,
                    "unknown": unknown,
                    "score": round(coupling_score, 6),
                },
                "overlap_group": ov.get(peak["id"]),
                "flags": flags,
                "status": "proposed",
                "algo_version": ALGO_VERSION,
                "param_fp": param_fp,
                "sources": sources,
            })
        # missing-peak case: keep an explicit unmatched candidate (unknown
        # state is first-class data, never silently forced onto a peak)
        if not any(c["shift_evidence"]["score"] >= 0.6 for c in atom_cands):
            cid = "cand:%s:none:%s:%s:%s" % (atom["id"], ALGO_VERSION, param_fp, src_fp)
            atom_cands.append({
                "candidate_id": cid,
                "atom_id": atom["id"],
                "peak_id": None,
                "nucleus": nucleus,
                "score": 0.0,
                "shift_evidence": None,
                "coupling_evidence": {"supporting": [], "missing": [],
                                      "unknown": [], "score": 0.0},
                "overlap_group": None,
                "flags": ["no_matching_peak"],
                "status": "unmatched",
                "algo_version": ALGO_VERSION,
                "param_fp": param_fp,
                "sources": sources,
            })
        # tie pass: equal-best candidates stay ambiguous, all are kept
        peak_cands = [c for c in atom_cands if c["peak_id"]]
        if peak_cands:
            top = max(c["score"] for c in peak_cands)
            tops = [c for c in peak_cands if top - c["score"] <= tie_eps]
            if len(tops) > 1:
                for c in tops:
                    c["flags"].append("tied")
                    c["status"] = "ambiguous"
        atom_cands.sort(key=lambda c: (-c["score"], c["peak_id"] or "~"))
        candidates.extend(atom_cands)
    return candidates
