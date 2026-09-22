"""Assignment candidate engine.

Principles encoded here:
- Overlapping peaks and missing peaks are NEVER forced into a unique match;
  all legal candidates are kept and flagged.
- The coupling network (COSY/HSQC/HMBC) is evidence only: it raises or lowers
  scores, it never deletes a candidate.
- Every derived value carries the algorithm version and a fingerprint of the
  parameters (incl. solvent / temperature / reference standard), so a change
  of conditions only stales the dependent candidate set; old sets and old
  decisions remain replayable.
"""

import hashlib
import json
from typing import Dict, List

import numpy as np

ALGO_VERSION = "assign-1.0.0"

DEFAULT_PARAMS = {
    "tol_H": 0.30,
    "tol_C": 2.0,
    "overlap_H": 0.03,
    "overlap_C": 0.20,
    "shift_offset_H": 0.0,
    "shift_offset_C": 0.0,
    "solvent": "",
    "temperature_K": None,
    "reference": "",
}

EVIDENCE_WEIGHT = {"HSQC": 0.30, "COSY": 0.15, "HMBC": 0.15}
TIE_EPS = 1e-6


def param_fingerprint(params: Dict) -> str:
    blob = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _expected_relations(atoms, bonds):
    """Expected coupling relations between atoms from the bond graph.

    Returns list of (kind, atom_a, atom_b):
    - HSQC: H directly bonded to C
    - COSY: H atoms on adjacent carbons (3-bond H-C-C-H)
    - HMBC: H to C within 2-3 bonds
    """
    adj = {a.id: set() for a in atoms}
    for b in bonds:
        adj[b.a].add(b.b)
        adj[b.b].add(b.a)
    by_id = {a.id: a for a in atoms}
    relations = []

    def paths(u, v, maxlen):
        out, stack = [], [(u, [u])]
        while stack:
            node, path = stack.pop()
            if len(path) > maxlen:
                continue
            if node == v and len(path) > 1:
                out.append(path)
                continue
            for nxt in adj[node]:
                if nxt not in path:
                    stack.append((nxt, path + [nxt]))
        return out

    seen = set()
    for a in atoms:
        for b in atoms:
            if a.id >= b.id:
                continue
            key = (a.id, b.id)
            if key in seen:
                continue
            p2 = paths(a.id, b.id, 2)
            p3 = paths(a.id, b.id, 3)
            p4 = paths(a.id, b.id, 4)
            ea, eb = a.element, b.element
            if {ea, eb} == {"H", "C"} and p2:
                relations.append(("HSQC", a.id, b.id))
                seen.add(key)
            elif ea == "H" and eb == "H" and p4:
                relations.append(("COSY", a.id, b.id))
                seen.add(key)
            elif {ea, eb} == {"H", "C"} and (p3 or p4) and not p2:
                relations.append(("HMBC", a.id, b.id))
                seen.add(key)
    return relations


def generate_candidates(atoms, bonds, peaks, edges, params) -> Dict:
    p = dict(DEFAULT_PARAMS)
    p.update(params)
    fp = param_fingerprint(p)

    usable = [pk for pk in peaks if pk.shift.value is not None]
    skipped = [pk.id for pk in peaks if pk.shift.value is None]
    by_id = {pk.id: pk for pk in usable}

    # --- overlap groups: same-nucleus peaks closer than the threshold ---
    findings = []
    overlap_of: Dict[str, List[str]] = {}
    for nucleus, tol_key in (("H", "overlap_H"), ("C", "overlap_C")):
        group_peaks = sorted((pk for pk in usable if pk.nucleus == nucleus),
                             key=lambda pk: pk.shift.value)
        arr = np.array([pk.shift.value for pk in group_peaks])
        for i in range(len(group_peaks)):
            close = np.where(np.abs(arr - arr[i]) <= p[tol_key])[0]
            mates = sorted(group_peaks[j].id for j in close if j != i)
            if mates:
                overlap_of[group_peaks[i].id] = mates
    for pid, mates in sorted(overlap_of.items()):
        findings.append({
            "type": "overlap_not_forced",
            "detail": f"peak {pid} overlaps {mates}; no unique match is forced",
            "peaks": [pid] + mates,
        })

    # --- expected vs observed coupling edges ---
    relations = _expected_relations(atoms, bonds)
    actual = {(e.kind, e.from_peak, e.to_peak) for e in edges}
    actual |= {(k, b, a) for (k, a, b) in actual}

    def edge_between(kind, peak_a, peak_b):
        return (kind, peak_a, peak_b) in actual

    # --- candidate generation ---
    candidates: List[Dict] = []
    for atom in atoms:
        tol = p["tol_H"] if atom.element == "H" else p["tol_C"]
        offset = p["shift_offset_H"] if atom.element == "H" else p["shift_offset_C"]
        center = 0.5 * (atom.shift_min + atom.shift_max)
        half = 0.5 * (atom.shift_max - atom.shift_min) + tol
        atom_cands = []
        for pk in usable:
            if pk.nucleus != atom.element:
                continue
            eff = pk.shift.value - offset
            if not (atom.shift_min - tol <= eff <= atom.shift_max + tol):
                continue
            base = max(0.0, 1.0 - abs(eff - center) / half)
            evidence = []
            bonus = 0.0
            for (kind, a_id, b_id) in relations:
                if atom.id not in (a_id, b_id):
                    continue
                other_atom = b_id if atom.id == a_id else a_id
                for other_pk in usable:
                    if other_pk.id == pk.id:
                        continue
                    if edge_between(kind, pk.id, other_pk.id):
                        bonus += EVIDENCE_WEIGHT[kind]
                        evidence.append({
                            "kind": kind, "to_atom": other_atom,
                            "via_peak": other_pk.id,
                        })
            score = round(min(1.0, base + min(bonus, 0.45)), 6)
            atom_cands.append({
                "id": f"{atom.id}~{pk.id}",
                "atom_id": atom.id,
                "peak_id": pk.id,
                "score": score,
                "base": round(base, 6),
                "evidence": evidence,
                "overlap": pk.id in overlap_of,
                "tied": False,
                "missing_evidence": False,
                "status": "proposed",
            })
        # ties: keep every equally-best candidate
        if atom_cands:
            best = max(c["score"] for c in atom_cands)
            tied = [c for c in atom_cands if abs(c["score"] - best) < TIE_EPS]
            if len(tied) > 1:
                for c in tied:
                    c["tied"] = True
                findings.append({
                    "type": "tied_candidates",
                    "detail": f"atom {atom.id} has {len(tied)} equally-best "
                              f"candidates; all are kept",
                    "atom": atom.id,
                    "candidates": [c["id"] for c in tied],
                })
        candidates.extend(atom_cands)

    # --- missing edges: evidence expected but absent anywhere ---
    by_atom = {}
    for c in candidates:
        by_atom.setdefault(c["atom_id"], []).append(c)
    for (kind, a_id, b_id) in relations:
        found = False
        for ca in by_atom.get(a_id, []):
            for cb in by_atom.get(b_id, []):
                if edge_between(kind, ca["peak_id"], cb["peak_id"]):
                    found = True
        if not found:
            findings.append({
                "type": "missing_edge",
                "detail": f"expected {kind} relation {a_id}<->{b_id} has no "
                          f"observed edge; candidates keep unknown-evidence state",
                "kind": kind, "atoms": [a_id, b_id],
            })
            for c in by_atom.get(a_id, []) + by_atom.get(b_id, []):
                c["missing_evidence"] = True

    # --- symmetry groups share candidate peaks ---
    sym_groups: Dict[str, List[str]] = {}
    for atom in atoms:
        if atom.symmetry_group:
            sym_groups.setdefault(atom.symmetry_group, []).append(atom.id)
    for grp, members in sorted(sym_groups.items()):
        if len(members) > 1:
            findings.append({
                "type": "symmetry_group",
                "detail": f"atoms {members} are symmetry-equivalent; "
                          f"candidates mirror each other",
                "atoms": members,
            })

    if skipped:
        findings.append({
            "type": "unconvertible_values",
            "detail": f"peaks {skipped} have unconvertible shifts; kept raw "
                      f"with diagnostics, excluded from scoring",
            "peaks": skipped,
        })

    return {
        "algo": ALGO_VERSION,
        "fingerprint": fp,
        "params": p,
        "candidates": sorted(candidates, key=lambda c: (c["atom_id"], -c["score"], c["peak_id"])),
        "findings": findings,
        "relations": [list(r) for r in relations],
    }
