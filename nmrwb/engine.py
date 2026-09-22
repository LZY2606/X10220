"""Domain engine: assignment-candidate generation for NMR peak lists.

Design rules encoded here (also emitted as machine-readable guard events):

* Overlapping or missing peaks are NEVER forced into a unique match; all
  legal candidates are kept and the atom is flagged ambiguous/unknown.
* The coupling network (COSY/HSQC/HMBC) is used as *evidence* only: edges
  adjust scores, they never add hard constraints.
* Environment changes (solvent / temperature / reference standard) only
  stale the candidates that depend on them; old decisions stay replayable.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

ALGO_VERSION = "candgen/1.0"

PARAM_DEFAULTS = {
    "tol_h_ppm": 0.05,
    "tol_c_ppm": 0.5,
    "tie_eps": 1e-9,
    "edge_weight": 0.25,
    "offset_guard_ppm": 0.03,
    "reference": "TMS",
    "solvent": "CDCl3",
    "temperature_k": 298.0,
}

# Standing policy judgements, attached to every compute event so the
# archive can replay *why* the engine refused to force unique matches.
POLICY_GUARDS = [
    {"rule": "no-forced-unique-match",
     "scope": "overlapping-peaks+missing-peaks",
     "effect": "all legal candidates retained; atom flagged ambiguous/unknown"},
    {"rule": "coupling-network-is-evidence-only",
     "scope": "COSY/HSQC/HMBC",
     "effect": "edges adjust scores; they never add or remove candidates"},
    {"rule": "environment-change-stales-only-dependents",
     "scope": "solvent/temperature_k/reference",
     "effect": "dependent candidates become stale; old decisions replayable"},
]

# which parameters affect which derived atoms
PARAM_SCOPE = {
    "tol_h_ppm": "proton",
    "tol_c_ppm": "carbon",
    "edge_weight": "all",
    "tie_eps": "all",
    "offset_guard_ppm": "none",
    "reference": "all",
    "solvent": "all",
    "temperature_k": "all",
}


def fingerprint(params):
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _bond_distances(molecule):
    """BFS all-pairs bond distances over the skeleton (numpy adjacency)."""
    atoms = [a["id"] for a in molecule["atoms"]]
    idx = {a: i for i, a in enumerate(atoms)}
    n = len(atoms)
    adj = np.zeros((n, n), dtype=int)
    for x, y in molecule.get("bonds", []):
        adj[idx[x], idx[y]] = 1
        adj[idx[y], idx[x]] = 1
    dist = np.full((n, n), 99, dtype=int)
    for s in range(n):
        dist[s, s] = 0
        frontier = [s]
        d = 0
        while frontier:
            d += 1
            nxt = []
            for u in frontier:
                for v in range(n):
                    if adj[u, v] and dist[s, v] == 99:
                        dist[s, v] = d
                        nxt.append(v)
            frontier = nxt
    return {a: {b: int(dist[idx[a], idx[b]]) for b in atoms} for a in atoms}


def generate_candidates(molecule, parsed, params):
    """Build per-atom assignment candidates.

    Returns {"atoms": {atom_id: {...}}, "guards": [...]} where every atom
    record carries stable candidate ids, scores, evidence, rejection/unknown
    reasons and the dependency declaration used for invalidation.
    """
    rng = np.random.default_rng(0)  # fixed seed; engine itself is deterministic
    _ = rng  # reserved for tie-stable ordering
    dist = _bond_distances(molecule)
    tol_h = float(params["tol_h_ppm"])
    tol_c = float(params["tol_c_ppm"])
    ew = float(params["edge_weight"])
    tie_eps = float(params["tie_eps"])

    protons = [p for p in parsed["protons"] if p["shift_ppm"] is not None]
    carbons = [c for c in parsed["carbons"] if c["shift_ppm"] is not None]
    guards = list(POLICY_GUARDS)

    p_sites = molecule.get("proton_sites", [])
    c_sites = molecule.get("carbon_sites", [])
    attached_of = {s["id"]: s["attached"] for s in p_sites}

    # ---- raw membership (shift window only; coupling never filters) ----
    def h_members(expected):
        out = []
        for p in protons:
            err = p["shift_ppm"] - expected
            if abs(err) <= tol_h:
                out.append((p, err))
        return out

    def c_members(expected):
        out = []
        for c in carbons:
            err = c["shift_ppm"] - expected
            if abs(err) <= tol_c:
                out.append((c, err))
        return out

    h_mem = {s["id"]: h_members(float(s["expected_ppm"])) for s in p_sites}
    c_mem = {s["id"]: c_members(float(s["expected_ppm"])) for s in c_sites}
    h_peak_ids = {s: {p["id"] for p, _ in m} for s, m in h_mem.items()}
    c_peak_ids = {s: {c["id"] for c, _ in m} for s, m in c_mem.items()}

    # ---- overlap guard: peaks whose windows collide can never be forced ----
    overlapped_peaks = set()
    for i in range(len(protons)):
        for j in range(i + 1, len(protons)):
            if abs(protons[i]["shift_ppm"] - protons[j]["shift_ppm"]) <= tol_h:
                overlapped_peaks.add(protons[i]["id"])
                overlapped_peaks.add(protons[j]["id"])
    if overlapped_peaks:
        affected = sorted(s for s, ids in h_peak_ids.items() if ids & overlapped_peaks)
        guards.append({
            "rule": "overlap-no-forced-unique",
            "peaks": sorted(overlapped_peaks),
            "atoms": affected,
            "effect": "atoms kept ambiguous; every legal candidate retained",
        })

    # ---- evidence from the coupling network (score-only) ----
    def h_evidence(site_id, peak_id):
        site = next(s for s in p_sites if s["id"] == site_id)
        attached = site["attached"]
        ev = []
        for e in parsed["hsqc"]:
            if e["a"] == peak_id and any(e["b"] in c_peak_ids[cs["id"]] for cs in c_sites if cs["id"] == attached):
                ev.append({"edge": e["id"], "kind": "HSQC", "supports": attached})
        for e in parsed["cosy"]:
            other = None
            if e["a"] == peak_id:
                other = e["b"]
            elif e["b"] == peak_id:
                other = e["a"]
            if other:
                for s2 in p_sites:
                    if s2["id"] != site_id and other in h_peak_ids[s2["id"]] \
                            and dist[attached][s2["attached"]] == 1:
                        ev.append({"edge": e["id"], "kind": "COSY", "supports": s2["id"]})
        for e in parsed["hmbc"]:
            if e["a"] == peak_id:
                for cs in c_sites:
                    if e["b"] in c_peak_ids[cs["id"]] and 1 <= dist[attached][cs["id"]] <= 3:
                        ev.append({"edge": e["id"], "kind": "HMBC", "supports": cs["id"]})
        return ev

    def c_evidence(site_id, peak_id):
        ev = []
        for e in parsed["hsqc"]:
            if e["b"] == peak_id:
                for s2 in p_sites:
                    if s2["attached"] == site_id and e["a"] in h_peak_ids[s2["id"]]:
                        ev.append({"edge": e["id"], "kind": "HSQC", "supports": s2["id"]})
        for e in parsed["hmbc"]:
            if e["b"] == peak_id:
                for s2 in p_sites:
                    if e["a"] in h_peak_ids[s2["id"]] and 1 <= dist[s2["attached"]][site_id] <= 3:
                        ev.append({"edge": e["id"], "kind": "HMBC", "supports": s2["id"]})
        return ev

    atoms = {}

    # ---- proton sites, grouped by symmetry ----
    sym_groups = {}
    for s in p_sites:
        sym_groups.setdefault(s.get("sym") or s["id"], []).append(s["id"])

    for sym, members in sym_groups.items():
        # symmetry-equivalent sites share one merged candidate pool
        pool = {}
        for sid in members:
            for p, err in h_mem[sid]:
                key = p["id"]
                if key not in pool or abs(err) < abs(pool[key][1]):
                    pool[key] = (p, err)
        degenerate = len(members) > 1
        if degenerate:
            guards.append({
                "rule": "symmetry-degenerate",
                "group": sym,
                "atoms": sorted(members),
                "effect": "symmetry-equivalent nuclei share candidates; no unique per-atom match",
            })
        for sid in members:
            cands = []
            for pid, (p, err) in sorted(pool.items()):
                ev = h_evidence(sid, pid)
                base = max(0.0, 1.0 - abs(err) / tol_h)
                score = round(base + ew * len(ev), 6)
                cands.append({
                    "candidate_id": "%s~%s" % (sid, pid),
                    "peak_id": pid,
                    "shift_ppm": p["shift_ppm"],
                    "shift_raw": p.get("shift_raw"),
                    "error_ppm": round(err, 6),
                    "multiplicity": p.get("multiplicity", ""),
                    "integration": p.get("integration"),
                    "evidence": ev,
                    "score": score,
                    "overlapped_peak": pid in overlapped_peaks,
                })
            cands.sort(key=lambda c: (-c["score"], c["peak_id"]))
            atoms[sid] = _atom_record(
                sid, "proton", cands, tie_eps, degenerate,
                overlapped_peaks, guards,
                depends_sections=_sections_for("proton", cands),
            )

    # ---- carbon sites ----
    for s in c_sites:
        sid = s["id"]
        cands = []
        for c, err in c_mem[sid]:
            ev = c_evidence(sid, c["id"])
            base = max(0.0, 1.0 - abs(err) / tol_c)
            score = round(base + ew * len(ev), 6)
            cands.append({
                "candidate_id": "%s~%s" % (sid, c["id"]),
                "peak_id": c["id"],
                "shift_ppm": c["shift_ppm"],
                "shift_raw": c.get("shift_raw"),
                "error_ppm": round(err, 6),
                "evidence": ev,
                "score": score,
                "overlapped_peak": False,
            })
        cands.sort(key=lambda c: (-c["score"], c["peak_id"]))
        atoms[sid] = _atom_record(
            sid, "carbon", cands, tie_eps, False,
            overlapped_peaks, guards,
            depends_sections=_sections_for("carbon", cands),
        )

    # ---- global shift-offset guard (e.g. reference-standard change) ----
    for kind in ("proton", "carbon"):
        errs = [rec["candidates"][0]["error_ppm"]
                for rec in atoms.values()
                if rec["kind"] == kind and rec["candidates"]]
        if errs:
            median_err = float(np.median(np.array(errs)))
            if abs(median_err) > float(params["offset_guard_ppm"]):
                guards.append({
                    "rule": "global-shift-offset",
                    "nucleus": kind,
                    "median_error_ppm": round(median_err, 6),
                    "threshold_ppm": params["offset_guard_ppm"],
                    "effect": "likely reference-standard bias; candidates stale-able, not rewritten",
                })

    return {"atoms": atoms, "guards": guards}


def _sections_for(kind, cands):
    sections = {"1H" if kind == "proton" else "13C"}
    for c in cands:
        for ev in c["evidence"]:
            sections.add(ev["kind"])
    return sorted(sections)


def _atom_record(atom_id, kind, cands, tie_eps, degenerate, overlapped_peaks,
                 guards, depends_sections):
    rejected = []
    if not cands:
        status = "unknown"
        reason = "no peak inside tolerance; missing peak is not force-matched"
        guards.append({
            "rule": "missing-peak-not-forced",
            "atom": atom_id,
            "effect": "atom left unknown; zero candidates retained as first-class state",
        })
    else:
        reason = None
        top = cands[0]["score"]
        tied = [c for c in cands if abs(c["score"] - top) <= tie_eps]
        if degenerate:
            status = "symmetry_degenerate"
        elif len(tied) > 1:
            status = "tied"
            guards.append({
                "rule": "tied-best-candidates",
                "atom": atom_id,
                "candidates": [c["candidate_id"] for c in tied],
                "effect": "equal best scores; all tied candidates kept",
            })
        elif len(cands) > 1 or any(c["overlapped_peak"] for c in cands):
            status = "ambiguous"
        else:
            status = "unique"
        for c in cands[1:]:
            rejected.append({
                "candidate_id": c["candidate_id"],
                "reason": "lower score (%.6f < %.6f); retained, not discarded" % (c["score"], top),
            })
    return {
        "atom_id": atom_id,
        "kind": kind,
        "status": status,
        "reason": reason,
        "candidates": cands,
        "rejected": rejected,
        "depends_on": {
            "sections": depends_sections,
            "params": ["tol_h_ppm"] if kind == "proton" else ["tol_c_ppm"],
        },
    }
