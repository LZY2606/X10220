"""Candidate assignment engine.

Principles encoded here:
- overlapping peaks and missing peaks are never forced into unique matches;
- the coupling network (COSY/HSQC/HMBC) is evidence only, never a constraint;
- tied best candidates are all kept;
- symmetric (chemically equivalent) atoms are handled as degenerate groups;
- candidates, rejection reasons, errors and unknown states are first-class data.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

import numpy as np

from .units import normalize_peak, Quantity

ALGO_VERSION = "cand-1.0.0"

DEFAULT_PARAMS = {
    "shift_tol_ppm": 0.30,
    "shift_bias_ppm": 0.0,
    "integral_tol": 0.60,
    "tie_eps": 1e-9,
    "accept_score": 0.40,
}

# soft multiplicity agreement (unknown observed multiplicity stays neutral)
_MULT_MATCH = 1.0
_MULT_MISMATCH = 0.30
_MULT_UNKNOWN = 0.60


def param_fingerprint(params: dict) -> str:
    blob = json.dumps({"algo": ALGO_VERSION, "params": params}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def merge_params(overrides: Optional[dict]) -> dict:
    params = dict(DEFAULT_PARAMS)
    if overrides:
        for k, v in overrides.items():
            if k in params:
                params[k] = float(v) if k != "tie_eps" else float(v)
    return params


def _atom_center_span(atom: dict) -> tuple[float, float]:
    lo, hi = atom["expected_shift_ppm"]
    return (lo + hi) / 2.0, (hi - lo) / 2.0


def _mult_score(expected: str, observed: str) -> float:
    if not observed or observed == "unknown":
        return _MULT_UNKNOWN
    return _MULT_MATCH if expected == observed else _MULT_MISMATCH


def equivalence_groups(molecule: dict, nucleus: str) -> list[dict]:
    """Group atoms of the given nucleus by equivalence group (symmetry)."""
    element = {"1H": "H", "13C": "C"}[nucleus]
    groups: dict[str, list[dict]] = {}
    for atom in molecule["atoms"]:
        if atom["element"] != element:
            continue
        key = atom.get("equiv_group") or atom["atom_id"]
        groups.setdefault(key, []).append(atom)
    out = []
    for key, atoms in sorted(groups.items()):
        lo = min(a["expected_shift_ppm"][0] for a in atoms)
        hi = max(a["expected_shift_ppm"][1] for a in atoms)
        out.append({
            "group": key,
            "atom_ids": [a["atom_id"] for a in atoms],
            "degenerate": len(atoms) > 1,
            "expected_shift_ppm": [lo, hi],
            "expected_multiplicity": atoms[0].get("expected_multiplicity", "unknown"),
            "proton_count": sum(int(a.get("proton_count", 1)) for a in atoms),
        })
    return out


def _neighbor_groups(molecule: dict, group_atom_ids: list[str]) -> list[str]:
    atom_set = set(group_atom_ids)
    neigh: set[str] = set()
    equiv = {}
    for atom in molecule["atoms"]:
        equiv[atom["atom_id"]] = atom.get("equiv_group") or atom["atom_id"]
    for bond in molecule.get("bonds", []):
        a, b = bond
        if a in atom_set and b not in atom_set:
            neigh.add(equiv.get(b, b))
        if b in atom_set and a not in atom_set:
            neigh.add(equiv.get(a, a))
    # attached nuclei (C-H) count as neighbours for HSQC/HMBC evidence
    for atom in molecule["atoms"]:
        for h in atom.get("attached", []):
            if h in atom_set:
                neigh.add(equiv.get(atom["atom_id"], atom["atom_id"]))
            if atom["atom_id"] in atom_set:
                neigh.add(equiv.get(h, h))
    return sorted(neigh)


def generate_candidates(molecule: dict, peak_list: dict, correlations: list[dict],
                        conditions: dict, nucleus: str,
                        params: Optional[dict] = None,
                        source_ids: Optional[dict] = None) -> tuple[dict, list[str]]:
    """Build the candidate set payload for one nucleus.

    source_ids: {"molecule": inp_id, "peak_list": inp_id, "correlations": {type: inp_id},
                 "conditions": inp_id} used to record direct sources per candidate.
    Returns (payload, diagnostics)."""
    p = merge_params(params)
    src = source_ids or {}
    diags: list[str] = []

    peaks, peak_diags = [], []
    for raw_peak in peak_list["peaks"]:
        pk, ds = normalize_peak(raw_peak)
        peak_diags.extend(ds)
        peaks.append(pk)
    diags.extend(peak_diags)

    groups = equivalence_groups(molecule, nucleus)
    edges = []
    for corr in correlations:
        for e in corr["edges"]:
            edges.append((corr["type"], e["edge_id"], e["from_peak"], e["to_peak"]))

    # preliminary best peak per group (shift-only) used to look up evidence
    prelim: dict[str, Optional[str]] = {}
    for g in groups:
        center, span = _center_span_of(g)
        best, best_d = None, None
        for pk in peaks:
            if not pk.usable:
                continue
            d = abs(pk.shift.value - p["shift_bias_ppm"] - center)
            if best_d is None or d < best_d:
                best, best_d = pk.peak_id, d
        prelim[g["group"]] = best

    candidates, rejected = [], []
    for g in groups:
        center, span = _center_span_of(g)
        neigh = _neighbor_groups(molecule, g["atom_ids"])
        scored = []
        for pk in peaks:
            if not pk.usable:
                rejected.append({
                    "group": g["group"], "peak_id": pk.peak_id,
                    "reason": "unparseable_shift",
                    "detail": pk.shift.diagnostic,
                })
                continue
            eff = pk.shift.value - p["shift_bias_ppm"]
            d = abs(eff - center)
            shift_score = 1.0 if d <= span else max(0.0, 1.0 - (d - span) / p["shift_tol_ppm"])
            # integral: a peak may host several groups (overlap allowed), so it
            # only hurts when it integrates for LESS than this group needs
            integ = pk.integral.value or 0.0
            need = max(1e-9, float(g["proton_count"]))
            int_score = 1.0 if integ >= need * (1.0 - p["integral_tol"]) else max(
                0.0, integ / need)
            mult_score = _mult_score(g["expected_multiplicity"], pk.multiplicity)
            evidence = []
            for etype, eid, a, b in edges:
                other = None
                if a == pk.peak_id:
                    other = b
                elif b == pk.peak_id:
                    other = a
                if other is None:
                    continue
                for ng in neigh:
                    if prelim.get(ng) == other:
                        evidence.append({"edge_id": eid, "type": etype,
                                         "to_peak": other, "supports_group": ng})
            ev_bonus = min(0.15, 0.05 * len(evidence))
            score = float(np.round(
                0.50 * shift_score + 0.20 * int_score + 0.15 * mult_score + ev_bonus, 9))
            scored.append((score, pk, evidence))

        group_cands = []
        for score, pk, evidence in scored:
            sources = _candidate_sources(src, g, pk, evidence, nucleus)
            entry = {
                "candidate_id": f"{g['group']}->{pk.peak_id}",
                "group": g["group"],
                "atom_ids": g["atom_ids"],
                "degenerate": g["degenerate"],
                "peak_id": pk.peak_id,
                "score": score,
                "status": "proposed",
                "tied": False,
                "overlap": False,
                "evidence": evidence,
                "reasons": _reasons(g, pk, score, p),
                "sources": sources,
            }
            if score >= p["accept_score"]:
                group_cands.append(entry)
            else:
                rejected.append({"group": g["group"], "peak_id": pk.peak_id,
                                 "reason": "score_below_threshold",
                                 "detail": f"score={score:.3f} < {p['accept_score']}"})
        if not group_cands:
            candidates.append({
                "candidate_id": f"{g['group']}->NONE",
                "group": g["group"],
                "atom_ids": g["atom_ids"],
                "degenerate": g["degenerate"],
                "peak_id": None,
                "score": 0.0,
                "status": "missing_peak",
                "tied": False,
                "overlap": False,
                "evidence": [],
                "reasons": ["no peak above accept threshold; not forced to a unique match"],
                "sources": _candidate_sources(src, g, None, [], nucleus),
            })
            continue
        best = max(c["score"] for c in group_cands)
        for c in group_cands:
            if abs(c["score"] - best) <= p["tie_eps"] and len(group_cands) > 1:
                c["tied"] = True
        candidates.extend(group_cands)

    # overlap: one peak serving several groups is flagged, never resolved by force
    by_peak: dict[str, list[dict]] = {}
    for c in candidates:
        if c["peak_id"]:
            by_peak.setdefault(c["peak_id"], []).append(c)
    for peak_id, cs in by_peak.items():
        groups_using = {c["group"] for c in cs}
        if len(groups_using) > 1:
            for c in cs:
                c["overlap"] = True
                c["reasons"].append(
                    f"peak {peak_id} shared by groups {sorted(groups_using)}; overlap kept, not forced unique")

    payload = {
        "nucleus": nucleus,
        "algo_version": ALGO_VERSION,
        "params": p,
        "param_fingerprint": param_fingerprint(p),
        "candidates": sorted(candidates, key=lambda c: (c["group"], -c["score"], c["candidate_id"])),
        "rejected": rejected,
        "conditions_ref": {
            "solvent": conditions.get("solvent"),
            "temperature": conditions.get("temperature"),
            "reference_standard": conditions.get("reference_standard"),
            "instrument_mhz": conditions.get("instrument_mhz"),
        },
    }
    return payload, diags


def _center_span_of(group: dict) -> tuple[float, float]:
    lo, hi = group["expected_shift_ppm"]
    return (lo + hi) / 2.0, (hi - lo) / 2.0


def _reasons(group: dict, pk, score: float, p: dict) -> list[str]:
    rs = [f"shift {pk.shift.raw} vs expected {group['expected_shift_ppm']} ppm "
          f"(bias {p['shift_bias_ppm']} ppm)"]
    if pk.multiplicity == "unknown":
        rs.append("multiplicity unknown: neutral score")
    return rs


def _candidate_sources(src: dict, group: dict, pk, evidence: list, nucleus: str) -> list[str]:
    s = []
    mol = src.get("molecule")
    if mol:
        s += [f"{mol}#atom:{a}" for a in group["atom_ids"]]
    pl = src.get("peak_list")
    if pl and pk is not None:
        s.append(f"{pl}#peak:{pk.peak_id}")
    cond = src.get("conditions")
    if cond:
        s += [f"{cond}#field:solvent", f"{cond}#field:temperature",
              f"{cond}#field:reference_standard", f"{cond}#field:instrument_mhz"]
    corr = src.get("correlations", {})
    for ev in evidence:
        inp = corr.get(ev["type"])
        if inp:
            s.append(f"{inp}#edge:{ev['edge_id']}")
    return sorted(set(s))
