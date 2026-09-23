"""Deterministic candidate-generation engine.

Principles encoded here:
- overlapped or missing peaks are never forced into a unique match; every
  legal candidate is kept and flagged;
- the coupling network (COSY/HSQC/HMBC) is evidence only, never a filter;
- every derived value carries the algorithm version, a parameter fingerprint
  and its direct sources so any project can be replayed step by step.
"""
import hashlib
import json

import numpy as np

ALGO_VERSION = "nmr-assign-1.0.0"

DEFAULT_PARAMS = {
    "window_scale": 3.0,        # candidate window = window_scale * atom tolerance
    "overlap_tol_ppm": {"1H": 0.02, "13C": 0.10},
    "tie_epsilon": 0.01,
    "w_shift": 0.5,
    "w_integration": 0.2,
    "w_evidence": 0.3,
}


def fingerprint(params, input_ids):
    blob = json.dumps({"algo": ALGO_VERSION, "params": params,
                       "inputs": sorted(input_ids)},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _overlap_groups(peaks, params):
    """Group peaks of the same nucleus closer than the overlap tolerance."""
    groups = {}
    for nucleus in ("1H", "13C"):
        tol = params["overlap_tol_ppm"][nucleus]
        ordered = sorted(
            (p for p in peaks
             if p["nucleus"] == nucleus and p["shift_ppm"] is not None),
            key=lambda p: p["shift_ppm"])
        gid = 0
        prev = None
        for p in ordered:
            if prev is None or p["shift_ppm"] - prev > tol:
                gid += 1
            prev = p["shift_ppm"]
            groups.setdefault(gid_key(nucleus, gid), []).append(p["peak_key"])
    return {g: ks for g, ks in groups.items() if len(ks) > 1}


def gid_key(nucleus, gid):
    return "ov-%s-%d" % (nucleus.replace(" ", ""), gid)


def compute_candidates(atoms, peaks, edges, params=None):
    """Return (candidates, meta).  Fully deterministic for the same inputs."""
    params = dict(DEFAULT_PARAMS, **(params or {}))
    atoms_by_id = {a["atom_id"]: a for a in atoms}
    peaks_by_key = {p["peak_key"]: p for p in peaks}
    overlaps = _overlap_groups(peaks, params)
    peak_overlap = {}
    for gid, keys in overlaps.items():
        for k in keys:
            peak_overlap[k] = {"group": gid, "members": sorted(keys)}

    edge_types_present = {e["edge_type"] for e in edges}
    edge_index = {}
    for e in edges:
        edge_index.setdefault((e["edge_type"], e["a_key"]), set()).add(e["b_key"])
        edge_index.setdefault((e["edge_type"], e["b_key"]), set()).add(e["a_key"])

    candidates = []
    for atom in atoms:
        nucleus = atom["nucleus"]
        window = params["window_scale"] * atom["tol_ppm"]
        pool = [p for p in peaks
                if p["nucleus"] == nucleus and p["shift_ppm"] is not None]
        if not pool:
            continue
        shifts = np.array([p["shift_ppm"] for p in pool], dtype=float)
        dist = np.abs(shifts - atom["predicted_ppm"])
        for p, d in zip(pool, dist.tolist()):
            if d > window:
                continue
            reasons = []
            shift_term = max(0.0, 1.0 - d / window)
            reasons.append({"term": "shift", "value": round(shift_term, 6),
                            "detail": "|%.4f - %.4f ppm| = %.4f ppm (window %.4f ppm)"
                                      % (p["shift_ppm"], atom["predicted_ppm"], d, window)})
            terms = [(params["w_shift"], shift_term)]

            if nucleus == "1H" and atom.get("expected_integration"):
                obs = p["integration"]
                if obs is None:
                    reasons.append({"term": "integration", "value": None,
                                    "detail": "integration unknown; term excluded"})
                else:
                    exp = atom["expected_integration"]
                    integ_term = max(0.0, 1.0 - abs(obs - exp) / max(exp, 1.0))
                    reasons.append({"term": "integration", "value": round(integ_term, 6),
                                    "detail": "observed %.2f H vs expected %.2f H"
                                              % (obs, exp)})
                    terms.append((params["w_integration"], integ_term))

            expected_edges = atom.get("expected_edges") or []
            usable = [ee for ee in expected_edges
                      if ee["edge_type"] in edge_types_present]
            if expected_edges and not usable:
                reasons.append({"term": "evidence", "value": None,
                                "detail": "no correlation edges of the expected types "
                                          "are present; evidence unknown, not rejected"})
            elif usable:
                support = 0
                for ee in usable:
                    other = atoms_by_id.get(ee["other_atom"])
                    if other is None:
                        continue
                    other_window = params["window_scale"] * other["tol_ppm"]
                    hit = False
                    for other_key in edge_index.get((ee["edge_type"], p["peak_key"]), ()):
                        op = peaks_by_key.get(other_key)
                        if (op is not None and op["shift_ppm"] is not None
                                and abs(op["shift_ppm"] - other["predicted_ppm"]) <= other_window):
                            hit = True
                            break
                    if hit:
                        support += 1
                ev_term = support / float(len(usable))
                missing = len(usable) - support
                detail = "%d/%d expected %s correlations supported" % (
                    support, len(usable),
                    "/".join(sorted({ee["edge_type"] for ee in usable})))
                if missing:
                    detail += "; %d expected correlation(s) absent -> evidence gap, kept as candidate" % missing
                reasons.append({"term": "evidence", "value": round(ev_term, 6),
                                "detail": detail})
                terms.append((params["w_evidence"], ev_term))

            wsum = sum(w for w, _ in terms)
            score = sum(w * v for w, v in terms) / wsum if wsum else 0.0
            flags = []
            if p["peak_key"] in peak_overlap:
                ov = peak_overlap[p["peak_key"]]
                flags.append("overlap")
                reasons.append({"term": "overlap", "value": None,
                                "detail": "peak belongs to overlap group %s %s; "
                                          "unique match is not enforced"
                                          % (ov["group"], ov["members"])})
            if atom.get("symmetry_group"):
                flags.append("symmetric")
            candidates.append({
                "atom_id": atom["atom_id"],
                "nucleus": nucleus,
                "peak_key": p["peak_key"],
                "score": round(score, 6),
                "status": "pending",
                "flags": sorted(set(flags)),
                "reasons": reasons,
                "sources": [],  # filled by caller with input ids
            })

    # tie detection per atom
    by_atom = {}
    for c in candidates:
        by_atom.setdefault(c["atom_id"], []).append(c)
    for atom_id, group in by_atom.items():
        group.sort(key=lambda c: (-c["score"], c["peak_key"]))
        if len(group) > 1:
            top = group[0]["score"]
            tied = [c for c in group if abs(c["score"] - top) <= params["tie_epsilon"]]
            if len(tied) > 1:
                for c in tied:
                    if "tied" not in c["flags"]:
                        c["flags"].append("tied")
                        c["flags"].sort()
                    c["reasons"].append({
                        "term": "tie", "value": None,
                        "detail": "tied best candidates %s; all legal candidates retained"
                                  % sorted(x["peak_key"] for x in tied)})
    candidates.sort(key=lambda c: (c["atom_id"], -c["score"], c["peak_key"]))
    meta = {
        "algo_version": ALGO_VERSION,
        "params": params,
        "overlap_groups": {g: sorted(ks) for g, ks in sorted(overlaps.items())},
    }
    return candidates, meta
