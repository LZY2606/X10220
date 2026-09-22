"""Assignment candidate engine.

First-class outputs (never just one final number):
  * candidate per (nucleus, peak) with shift residual, edge evidence, score;
  * explicit rejection reasons (tolerance, missing evidence, unknown);
  * overlap groups (peaks that cannot be forced to a unique nucleus);
  * symmetry groups (nuclei the data cannot distinguish);
  * missing expected correlation edges (coupling network is *evidence only*);
  * a fitted global shift bias (median residual), shown rather than hidden;
  * ranked top candidates with deterministic tie ordering ("joint best").

Every derived result carries algorithm version, parameter fingerprint and
direct source references.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

import numpy as np

from .fingerprint import fingerprint
from .importer import SourceDocument

ALGORITHM_VERSION = "assignment-1.3.0"

DEFAULT_PARAMS = {
    "shift_tolerance": {"1H": 0.10, "13C": 0.50},  # ppm (after global bias)
    "edge_match_tolerance": 0.015,                 # ppm for 2D axes
    "overlap_tolerance": 0.015,                    # ppm for 1D peaks
    "edge_bonus": {"HSQC": 0.35, "HMBC": 0.08, "COSY": 0.08},
    "edge_miss_penalty": 0.02,
    "min_bonus_evidence": {"HSQC": 1},
    "use_global_offset": True,
}


@dataclass
class Candidate:
    nucleus_id: str
    peak_id: Optional[str]           # None => unassigned / missing peak
    score: float
    shift_residual: Optional[float]
    global_offset_ppm: Optional[float]
    edge_evidence: List[Dict[str, Any]] = field(default_factory=list)
    supported_edges: List[str] = field(default_factory=list)
    rejected_reasons: List[str] = field(default_factory=list)
    status: str = "candidate"        # candidate | rejected | unknown
    rank: int = 0
    tied_best: bool = False
    ambiguity_group: Optional[List[str]] = None
    symmetry_group: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "nucleus_id": self.nucleus_id,
            "peak_id": self.peak_id,
            "score": round(self.score, 6),
            "shift_residual": (round(self.shift_residual, 6)
                               if self.shift_residual is not None else None),
            "global_offset_ppm": (round(self.global_offset_ppm, 6)
                                  if self.global_offset_ppm is not None else None),
            "edge_evidence": self.edge_evidence,
            "supported_edges": self.supported_edges,
            "rejected_reasons": self.rejected_reasons,
            "status": self.status,
            "rank": self.rank,
            "tied_best": self.tied_best,
            "ambiguity_group": self.ambiguity_group,
            "symmetry_group": self.symmetry_group,
        }


def _canon(doc: SourceDocument, q):
    return q.canonical_value if q is not None else None


def _group_overlapping_peaks(peaks, tol):
    used = set()
    groups = []
    vals = [(p.id, p.shift.canonical_value) for p in peaks
            if p.shift.canonical_value is not None]
    for pid, v in vals:
        if pid in used:
            continue
        group = [q for q, w in vals if abs(w - v) <= tol]
        if len(group) > 1:
            groups.append(sorted(group))
            used.update(group)
    return groups


def _atom_map(docs: List[SourceDocument]):
    atoms, bonds = {}, []
    for d in docs:
        if d.kind == "skeleton":
            for a in d.atoms:
                atoms[a.id] = a
            bonds.extend(d.bonds)
    return atoms, bonds


def _neighbor_graph(atoms, bonds):
    adj = {a: set() for a in atoms}
    bond_index = {}
    for bond in bonds:
        a, b = bond.a, bond.b
        if a in atoms and b in atoms:
            adj[a].add(b); adj[b].add(a)
            bond_index[frozenset((a, b))] = bond.order
    return adj, bond_index


def _nucleus_bonds(adj, bond_index):
    """Bonds between NMR-visible nuclei, using heavy-atom path distances."""
    nadj = {a: {} for a in adj}
    nuclei = list(adj)
    for i, x in enumerate(nuclei):
        # BFS heavy-atom distance through the full skeleton
        dist = {x: 0}
        frontier = [x]
        while frontier:
            nxt = []
            for cur in frontier:
                for nb in adj[cur]:
                    if nb not in dist:
                        dist[nb] = dist[cur] + 1
                        nxt.append(nb)
            frontier = nxt
        for y, d in dist.items():
            if y != x:
                nadj[x][y] = d
    return nadj


# --- expected 2D relations from the molecular skeleton --------------------

def expected_relations(atoms, bonds):
    """Return dict[experiment] -> set of "nucA|nucB" expected relations."""
    adj, _ = _neighbor_graph(atoms, bonds)
    dist = _nucleus_bonds(adj, {})
    rel = {"COSY": set(), "HSQC": set(), "HMBC": set()}
    ids = sorted(atoms)
    for i, x in enumerate(ids):
        ax = atoms[x]
        for y in ids[i + 1:]:
            ay = atoms[y]
            d = dist[x].get(y, 99)
            if ax.nucleus == "1H" and ay.nucleus == "1H" and d <= 3:
                rel["COSY"].add(f"{x}|{y}")
            if {ax.nucleus, ay.nucleus} == {"1H", "13C"}:
                if d == 1:
                    rel["HSQC"].add(f"{x}|{y}")
                if d in (2, 3):
                    rel["HMBC"].add(f"{x}|{y}")
    return rel


def _match_axis(value, peak_values, tol):
    if value is None:
        return set()
    return {pid for pid, pv in peak_values.items()
            if pv is not None and abs(pv - value) <= tol}


def observed_edge_map(docs: List[SourceDocument], tol):
    """Map experiment -> edge index -> matched 1D peak-id sets on each axis."""
    one_d = {}
    for d in docs:
        if d.kind == "1d":
            nuc = d.meta.get("nucleus")
            one_d.setdefault(nuc, {})
            for p in d.peaks:
                one_d[nuc][p.id] = p.shift.canonical_value
    obs = {"COSY": [], "HSQC": [], "HMBC": []}
    for d in docs:
        if d.kind != "2d":
            continue
        exp = d.meta.get("experiment")
        if exp not in obs:
            continue
        for e in d.edges:
            a1 = _match_axis(e.shift_f1.canonical_value, one_d.get("1H", {}), tol)
            a2_h = _match_axis(e.shift_f2.canonical_value, one_d.get("1H", {}), tol)
            a2_c = _match_axis(e.shift_f2.canonical_value, one_d.get("13C", {}), tol)
            if exp == "COSY":
                axis_b = a2_h
            elif exp == "HSQC":
                axis_b = a2_c
            else:  # HMBC f2 usually carbon
                axis_b = a2_c if a2_c else a2_h
            obs[exp].append({
                "edge_id": e.id, "source_doc": d.doc_id, "version": d.version,
                "f1": e.shift_f1.canonical_value,
                "f2": e.shift_f2.canonical_value,
                "f1_peaks": sorted(a1), "f2_peaks": sorted(axis_b),
            })
    return obs, one_d


def _edge_evidence_for(nucleus, peak_id, atoms, obs_map, params, assigned_peaks):
    """Evidence entries tying (nucleus, peak) to observed correlation edges."""
    out = []
    supported = []
    ax = atoms[nucleus]
    for exp in ("HSQC", "HMBC", "COSY"):
        for e in obs_map.get(exp, []):
            hit_f1 = peak_id in e["f1_peaks"]
            hit_f2 = peak_id in e["f2_peaks"]
            if not (hit_f1 or hit_f2):
                continue
            # what nucleus partners does this edge plausibly connect to?
            partners = []
            axis = "f1" if hit_f1 else "f2"
            other_peaks = e["f2_peaks"] if axis == "f1" else e["f1_peaks"]
            for other_id, other in atoms.items():
                if other_id == nucleus:
                    continue
                # resolve the other side against current candidate peak map
                op = assigned_peaks.get(other_id)
                if op and op in other_peaks:
                    partners.append(other_id)
            out.append({
                "experiment": exp,
                "edge_id": e["edge_id"],
                "source_doc": e["source_doc"],
                "version": e["version"],
                "axis": axis,
                "plausible_partners": sorted(partners),
                "weight": params["edge_bonus"][exp],
            })
            supported.append(e["edge_id"])
    return out, sorted(set(supported))


def _axis_near_partner(nucleus, partner_nucleus, exp, other_value, offsets, tol_c=0.60):
    """Compare an observed partner-axis shift with predicted shift + bias."""
    pred = partner_nucleus.shift_value
    if pred is None or other_value is None:
        return False
    off = offsets.get(partner_nucleus.nucleus, 0.0) or 0.0
    t = tol_c if partner_nucleus.nucleus == "13C" else 0.03
    return abs(other_value - (pred + off)) <= t


def compute_analysis(documents, params=None):
    """Compute candidate network for the current document set and parameters."""
    params = {**DEFAULT_PARAMS, **(params or {})}
    atoms, bonds = _atom_map(documents)
    for a in atoms.values():
        a.shift_value = (a.predicted_shift.canonical_value
                         if a.predicted_shift is not None else None)
    expected = expected_relations(atoms, bonds)
    edge_tol = params["edge_match_tolerance"]
    obs_map, peak_values = observed_edge_map(documents, edge_tol)
    overlap_tol = params["overlap_tolerance"]

    source_refs = [{
        "doc_id": d.doc_id, "version": d.version,
        "content_hash": d.content_hash, "kind": d.kind,
        "condition": {k: d.meta.get(k) for k in
                      ("solvent", "temperature_raw", "reference",
                       "spectrometer_frequency_raw", "nucleus", "experiment")},
    } for d in documents]

    # --- observed 1D peaks per nucleus type and overlap groups ---
    peaks_by_nuc: Dict[str, List[Any]] = {}
    for d in documents:
        if d.kind == "1d":
            peaks_by_nuc.setdefault(d.meta["nucleus"], []).extend(d.peaks)
    overlaps = {}
    for nuc, peaks in peaks_by_nuc.items():
        for g in _group_overlapping_peaks(peaks, overlap_tol):
            overlaps[g[0]] = g
    peak_overlap = {pid: g for g in overlaps.values() for pid in g}

    # --- global shift bias (median nearest residual per nucleus type) ---
    offsets: Dict[str, float] = {}
    for nuc, peaks in peaks_by_nuc.items():
        vals = [p.shift.canonical_value for p in peaks
                if p.shift.canonical_value is not None]
        residuals = []
        for a in atoms.values():
            if a.nucleus != nuc or a.shift_value is None:
                continue
            near = min((abs(v - a.shift_value) for v in vals), default=None)
            if near is None:
                continue
            sign = min(vals, key=lambda v: abs(v - a.shift_value)) - a.shift_value
            residuals.append(sign)
        offsets[nuc] = float(np.median(residuals)) if residuals else 0.0
    if not params.get("use_global_offset", True):
        offsets = {k: 0.0 for k in offsets}

    def support(nucleus, peak_id, value):
        """Edges supporting candidate (nucleus, peak); evidence entries."""
        entries, ids = [], []
        for exp in ("HSQC", "HMBC", "COSY"):
            partners = {}
            for pair in expected[exp]:
                x, y = pair.split("|")
                if x == nucleus:
                    partners[y] = pair
                elif y == nucleus:
                    partners[x] = pair
            for e in obs_map.get(exp, []):
                # candidate peak on the appropriate axis
                on_f1 = value is not None and e["f1"] is not None and \
                    abs(e["f1"] - value) <= edge_tol
                on_f2 = value is not None and e["f2"] is not None and \
                    abs(e["f2"] - value) <= edge_tol
                if not (on_f1 or on_f2):
                    continue
                other_value = e["f2"] if on_f1 else e["f1"]
                matched = [pid for pid, atom in atoms.items()
                           if pid in partners and
                           _axis_near_partner(nucleus, atom, exp, other_value,
                                              offsets)]
                if matched:
                    ids.append(e["edge_id"])
                    entries.append({
                        "experiment": exp, "edge_id": e["edge_id"],
                        "source_doc": e["source_doc"], "version": e["version"],
                        "expected_partners": sorted(matched),
                        "weight": params["edge_bonus"][exp],
                    })
        # de-duplicate by edge id, keep strongest experiment weight
        best = {}
        for ent in entries:
            if ent["edge_id"] not in best or \
                    ent["weight"] > best[ent["edge_id"]]["weight"]:
                best[ent["edge_id"]] = ent
        return list(best.values()), sorted(best)

    # --- missing expected edges (assignment-independent diagnostics) ---
    missing_edges = []
    for exp in ("HSQC", "HMBC", "COSY"):
        for pair in sorted(expected[exp]):
            x, y = pair.split("|")
            ax, ay = atoms[x], atoms[y]
            vx = None if ax.shift_value is None else \
                ax.shift_value + offsets.get(ax.nucleus, 0.0)
            vy = None if ay.shift_value is None else \
                ay.shift_value + offsets.get(ay.nucleus, 0.0)
            found = None
            for e in obs_map.get(exp, []):
                a, b = e["f1"], e["f2"]
                if ax.nucleus == "13C":
                    a, b = b, a
                if None in (a, b, vx, vy):
                    continue
                tx = 0.60 if ax.nucleus == "13C" else 0.03
                ty = 0.60 if ay.nucleus == "13C" else 0.03
                if abs(a - vx) <= tx and abs(b - vy) <= ty:
                    found = e["edge_id"]
                    break
            if found is None:
                missing_edges.append({"experiment": exp, "between": [x, y],
                                      "expected_near_ppm": [vx, vy]})

    missing_by_nucleus: Dict[str, List[Dict]] = {}
    for m in missing_edges:
        for nid in m["between"]:
            missing_by_nucleus.setdefault(nid, []).append(m)

    # --- candidates per nucleus ---
    results: Dict[str, List[Candidate]] = {}
    for nid, atom in sorted(atoms.items()):
        nuc = atom.nucleus
        tol = params["shift_tolerance"].get(nuc, 0.2)
        cands: List[Candidate] = []
        for peak in peaks_by_nuc.get(nuc, []):
            obs = peak.shift.canonical_value
            pred = atom.shift_value
            off = offsets.get(nuc, 0.0)
            resid = None if (obs is None or pred is None) else obs - pred - off
            reasons = []
            if resid is None:
                reasons.append("unknown_shift_value")
            elif abs(resid) > tol:
                reasons.append("shift_outside_tolerance")
            ev, supported = support(nid, peak.id, obs)
            score = 0.0
            if resid is not None and abs(resid) <= tol:
                score = max(0.0, 1.0 - abs(resid) / tol)
            score += sum(x["weight"] for x in ev)
            for m in missing_by_nucleus.get(nid, []):
                if m["experiment"] == "HSQC":
                    score -= params["edge_miss_penalty"]
            c = Candidate(
                nucleus_id=nid, peak_id=peak.id, score=float(score),
                shift_residual=resid, global_offset_ppm=off,
                edge_evidence=ev, supported_edges=supported,
                rejected_reasons=reasons,
                status="rejected" if reasons else "candidate")
            g = peak_overlap.get(peak.id)
            if g:
                c.ambiguity_group = g
            if atom.symmetry_group:
                c.symmetry_group = atom.symmetry_group
            cands.append(c)
        viable = [c for c in cands if c.status == "candidate"]
        if viable:
            viable.sort(key=lambda c: (-c.score,
                                       abs(c.shift_residual or 1e9), c.peak_id))
            top = viable[0].score
            for rank, c in enumerate(viable, 1):
                c.rank = rank
                c.tied_best = abs(c.score - top) <= 1e-9
        # unassigned / missing peak pseudo-candidate
        reasons = []
        if not viable:
            reasons.append("no_peak_within_tolerance")
        if atom.exchangeable:
            reasons.append("exchangeable_signal_may_be_absent")
        for m in missing_by_nucleus.get(nid, []):
            reasons.append(f"missing_expected_{m['experiment']}:"
                           f"{'/'.join(m['between'])}")
        unk = Candidate(nucleus_id=nid, peak_id=None, score=0.0,
                        shift_residual=None,
                        global_offset_ppm=offsets.get(nuc, 0.0),
                        rejected_reasons=sorted(set(reasons)),
                        status="unknown")
        cands.append(unk)
        results[nid] = cands

    symmetry = {}
    for nid, atom in atoms.items():
        if atom.symmetry_group:
            symmetry.setdefault(atom.symmetry_group, []).append(nid)

    param_fp = fingerprint({
        "algorithm": ALGORITHM_VERSION,
        "params": params,
    })
    condition_fp = fingerprint(sorted(
        (d.doc_id, d.meta.get("solvent"), d.meta.get("temperature_raw"),
         d.meta.get("reference"), d.meta.get("spectrometer_frequency_raw"))
        for d in documents if d.kind == "1d"))

    return {
        "algorithm_version": ALGORITHM_VERSION,
        "parameters": params,
        "parameter_fingerprint": param_fp,
        "condition_fingerprint": condition_fp,
        "sources": source_refs,
        "global_offsets_ppm": {k: round(v, 6) for k, v in offsets.items()},
        "overlap_groups": [g for g in overlaps.values()],
        "symmetry_groups": symmetry,
        "missing_edges": missing_edges,
        "candidates": {nid: [c.to_json() for c in cs]
                       for nid, cs in results.items()},
        "skeleton": {
            "atoms": [a.to_json() for a in atoms.values()],
            "bonds": [b.to_json() for b in bonds],
        },
        "peaks": {nuc: [p.to_json() for p in pl]
                  for nuc, pl in peaks_by_nuc.items()},
        "observed_edges": obs_map,
    }
