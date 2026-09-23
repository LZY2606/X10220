"""Assignment candidate engine (algo version ``shiftgraph-1.0``).

Design rules baked into the engine:
- Overlapping or missing peaks are never forced into a unique match; every
  legal candidate survives with flags/reasons as first-class data.
- The coupling network (COSY/HSQC/HMBC) is evidence only: it moves scores and
  adds reasons, it never creates or deletes candidates by itself.
- Every derived value carries the algorithm version, the parameter fingerprint
  and the direct source ids it was computed from.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

ALGO_VERSION = "shiftgraph-1.0"

DEFAULT_PARAMS = {
    "solvent": "CDCl3",
    "frequency_mhz": 400.0,
    "reference": "TMS",
    "temperature_K": 298.0,
    "shift_offset_1h": 0.0,
    "shift_offset_13c": 0.0,
    "tolerance_1h": 0.35,
    "tolerance_13c": 1.5,
    "tie_epsilon": 1e-6,
    "threshold": 0.25,
    "weights": {"shift": 0.4, "cosy": 0.2, "hsqc": 0.15, "hmbc": 0.15, "mult": 0.1},
}

# Which parameter keys, when changed, expire which elements' candidates.
# frequency_mhz is metadata-only: it is preserved but expires nothing.
PARAM_SCOPE = {
    "shift_offset_1h": {"H"},
    "tolerance_1h": {"H"},
    "shift_offset_13c": {"C"},
    "tolerance_13c": {"C"},
    "solvent": {"H", "C"},
    "temperature_K": {"H", "C"},
    "reference": {"H", "C"},
    "weights": {"H", "C"},
    "threshold": {"H", "C"},
    "tie_epsilon": {"H", "C"},
}


def params_fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def merged_params(payload: dict | None) -> dict:
    merged = dict(DEFAULT_PARAMS)
    if payload:
        merged.update(payload)
        merged["weights"] = {**DEFAULT_PARAMS["weights"], **payload.get("weights", {})}
    return merged


# ---------------------------------------------------------------- skeleton graph

def _neighbors(atom_id, bonds):
    for a, b in bonds:
        if a == atom_id:
            yield b
        elif b == atom_id:
            yield a


def carbon_of(atom_id, atoms, bonds):
    for nb in _neighbors(atom_id, bonds):
        if atoms[nb]["element"] == "C":
            return nb
    return None


def cosy_partners(atom_id, atoms, bonds):
    """H atoms attached to carbons directly bonded to this H's carbon."""
    c = carbon_of(atom_id, atoms, bonds)
    if c is None:
        return []
    partners = []
    for c2 in _neighbors(c, bonds):
        if atoms[c2]["element"] != "C":
            continue
        for h in _neighbors(c2, bonds):
            if atoms[h]["element"] == "H" and h != atom_id:
                partners.append(h)
    return sorted(partners)


def hmbc_targets(atom_id, atoms, bonds):
    """Carbons 2-3 bonds away from this H's carbon (through the C skeleton)."""
    c = carbon_of(atom_id, atoms, bonds)
    if c is None:
        return []
    dist = {c: 0}
    queue = [c]
    while queue:
        cur = queue.pop(0)
        if dist[cur] >= 3:
            continue
        for nb in _neighbors(cur, bonds):
            if atoms[nb]["element"] == "C" and nb not in dist:
                dist[nb] = dist[cur] + 1
                queue.append(nb)
    return sorted(k for k, d in dist.items() if 2 <= d <= 3)


def atom_dependencies(atom, atoms, bonds):
    """Source kinds this atom's candidates directly depend on."""
    if atom["element"] == "H":
        deps = {"peaks-1h", "edges-hsqc"}
        if cosy_partners(atom["id"], atoms, bonds):
            deps.add("edges-cosy")
        if hmbc_targets(atom["id"], atoms, bonds):
            deps.add("edges-hmbc")
        return deps
    return {"peaks-13c", "edges-hsqc", "edges-hmbc"}


# ---------------------------------------------------------------- scoring

def _corrected(peak_shift, offset):
    return None if peak_shift is None else peak_shift + offset


def _near(shift, mu, tol):
    return shift is not None and abs(shift - mu) <= tol


def generate_atom_candidates(atom, atoms, bonds, peaks_1h, peaks_13c, edges, params):
    """Generate candidate rows for one atom.

    ``peaks_*`` are lists of dicts with label/shift_ppm/multiplicity; ``edges``
    maps kind -> set of (a, b) label pairs. Returns (candidates, dep_kinds).
    """
    p = merged_params(params)
    w = p["weights"]
    is_h = atom["element"] == "H"
    peaks = peaks_1h if is_h else peaks_13c
    offset = p["shift_offset_1h"] if is_h else p["shift_offset_13c"]
    tol = p["tolerance_1h"] if is_h else p["tolerance_13c"]
    mu = atom["expected_shift"]
    deps = atom_dependencies(atom, atoms, bonds)

    partners = cosy_partners(atom["id"], atoms, bonds) if is_h else []
    targets = hmbc_targets(atom["id"], atoms, bonds) if is_h else []
    own_carbon = carbon_of(atom["id"], atoms, bonds) if is_h else None
    attached_hs = [h for h in _neighbors(atom["id"], bonds)
                   if atoms[h]["element"] == "H"] if not is_h else []

    usable = [pk for pk in peaks if pk["shift_ppm"] is not None]
    shifts = np.array([_corrected(pk["shift_ppm"], offset) for pk in usable], dtype=float)

    candidates = []
    for pk, corrected in zip(usable, shifts):
        err = abs(float(corrected) - mu)
        components, reasons = {}, []

        s_shift = max(0.0, 1.0 - err / tol)
        components["shift"] = round(s_shift, 6)
        components["shift_err_ppm"] = round(err, 6)

        if is_h and partners:
            hits = 0
            for partner in partners:
                p_mu = atoms[partner]["expected_shift"]
                ok = any(
                    _near(_corrected(q["shift_ppm"], offset), p_mu, tol)
                    and _has_edge(edges, "edges-cosy", pk["label"], q["label"])
                    for q in usable
                )
                if ok:
                    hits += 1
                else:
                    reasons.append(f"cosy_evidence_missing:{atoms[partner]['label']}")
            components["cosy"] = round(hits / len(partners), 6)
        elif is_h:
            components["cosy"] = None

        if is_h and targets:
            hits = 0
            for target in targets:
                t_mu = atoms[target]["expected_shift"]
                ok = any(
                    _near(q["shift_ppm"], t_mu, p["tolerance_13c"])
                    and _has_edge(edges, "edges-hmbc", pk["label"], q["label"], directed=True)
                    for q in peaks_13c if q["shift_ppm"] is not None
                )
                if ok:
                    hits += 1
                else:
                    reasons.append(f"hmbc_evidence_missing:{atoms[target]['label']}")
            components["hmbc"] = round(hits / len(targets), 6)
        elif is_h:
            components["hmbc"] = None

        if is_h and own_carbon is not None:
            c_mu = atoms[own_carbon]["expected_shift"]
            ok = any(
                _near(q["shift_ppm"], c_mu, p["tolerance_13c"])
                and _has_edge(edges, "edges-hsqc", pk["label"], q["label"])
                for q in peaks_13c if q["shift_ppm"] is not None
            )
            components["hsqc"] = 1.0 if ok else 0.0
            if not ok:
                reasons.append("hsqc_evidence_missing")
        elif not is_h and attached_hs:
            ok = any(
                _near(_corrected(q["shift_ppm"], p["shift_offset_1h"]),
                      atoms[h]["expected_shift"], p["tolerance_1h"])
                and _has_edge(edges, "edges-hsqc", q["label"], pk["label"])
                for h in attached_hs for q in peaks_1h if q["shift_ppm"] is not None
            )
            components["hsqc"] = 1.0 if ok else 0.0
            if not ok:
                reasons.append("hsqc_evidence_missing")

        if not is_h:
            ok = any(_has_edge(edges, "edges-hmbc", q["label"], pk["label"], directed=True)
                     for q in peaks_1h)
            components["hmbc"] = 1.0 if ok else 0.0
            if not ok:
                reasons.append("hmbc_evidence_missing")

        expected_mult = atom.get("expected_mult")
        if expected_mult:
            got = (pk.get("multiplicity") or "").strip()
            if not got:
                components["mult"] = 0.5
                reasons.append("mult_unknown")
            elif got == expected_mult:
                components["mult"] = 1.0
            else:
                components["mult"] = 0.0
                reasons.append(f"mult_mismatch:expected {expected_mult} got {got}")

        num, den = 0.0, 0.0
        for key, weight in w.items():
            value = components.get(key)
            if value is not None:
                num += weight * value
                den += weight
        score = num / den if den else 0.0

        if err <= tol:
            state = "candidate"
        elif err <= 3 * tol:
            state = "rejected"
            reasons = reasons + [f"shift_out_of_range:err={err:.3f}>tol={tol}"]
        else:
            continue
        candidates.append({
            "atom_id": atom["id"],
            "peak_label": pk["label"],
            "score": round(score, 6),
            "state": state,
            "reasons": reasons,
            "components": components,
        })

    if not any(c["state"] == "candidate" for c in candidates):
        candidates.append({
            "atom_id": atom["id"],
            "peak_label": None,
            "score": 0.0,
            "state": "unknown",
            "reasons": ["missing_peak:no peak within tolerance"],
            "components": {},
        })
    return candidates, sorted(deps)


def _has_edge(edges, kind, a, b, directed=False):
    pairs = edges.get(kind, set())
    if (a, b) in pairs:
        return True
    return False if directed else (b, a) in pairs


def apply_flags(all_candidates, atoms, tie_epsilon):
    """Post-pass: overlap / tied / symmetry flags across the whole atom set."""
    by_peak = {}
    for c in all_candidates:
        if c["state"] == "candidate" and c["peak_label"]:
            by_peak.setdefault(c["peak_label"], set()).add(c["atom_id"])
    for c in all_candidates:
        flags = list(c.get("flags", []))
        if c["state"] == "candidate" and c["peak_label"]:
            if len(by_peak.get(c["peak_label"], ())) > 1:
                flags.append("overlap")
        atom = atoms[c["atom_id"]]
        if atom.get("symmetry_group"):
            flags.append("symmetry_equivalent")
        c["flags"] = sorted(set(flags))
    by_atom = {}
    for c in all_candidates:
        if c["state"] == "candidate":
            by_atom.setdefault(c["atom_id"], []).append(c)
    for atom_id, group in by_atom.items():
        best = max(c["score"] for c in group)
        winners = [c for c in group if abs(c["score"] - best) <= tie_epsilon]
        if len(winners) > 1:
            for c in winners:
                c["flags"] = sorted(set(c["flags"]) | {"tied_best"})
    return all_candidates
