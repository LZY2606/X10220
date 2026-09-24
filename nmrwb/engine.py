"""Assignment candidate engine.

Principles encoded here:
- Overlapping or missing peaks are never forced into a unique match;
  every legal candidate is kept and annotated.
- The coupling network (COSY/HSQC/HMBC) is evidence only: present edges
  raise a score, missing edges are recorded as unknown evidence and
  never reject a candidate.
- Equal scores stay tied (same rank, shared tie group).
- Every candidate carries algo version, params fingerprint and direct
  source documents so expiry can be scoped to real dependencies.
"""
from __future__ import annotations

import hashlib
import json
import math

import numpy as np

ALGO_VERSION = "assign-1.0.0"
TIE_EPS = 1e-9


def params_fingerprint(values: dict) -> str:
    blob = json.dumps(values, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:12]


def _gauss(delta: float, tol: float) -> float:
    return float(math.exp(-0.5 * (delta / tol) ** 2))


def _adjacency(bonds: list[dict]) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for b in bonds:
        adj.setdefault(b["a"], set()).add(b["b"])
        adj.setdefault(b["b"], set()).add(b["a"])
    return adj


def _within(adj: dict[str, set[str]], start: str, lo: int, hi: int) -> set[str]:
    seen, frontier = {start}, {start}
    for _ in range(hi):
        nxt = set()
        for node in frontier:
            nxt |= adj.get(node, set())
        nxt -= seen
        seen |= nxt
        frontier = nxt
    out = set()
    # BFS again collecting shells lo..hi
    seen, frontier = {start}, {start}
    shells = []
    for _ in range(hi):
        nxt = set()
        for node in frontier:
            nxt |= adj.get(node, set())
        nxt -= seen
        seen |= nxt
        frontier = nxt
        shells.append(nxt)
    for shell in shells[lo - 1:]:
        out |= shell
    return out


def run_assignment(peaks: list[dict], edges: list[dict], missing: list[dict],
                   atoms: list[dict], bonds: list[dict], nucleus: str,
                   params: dict, src_docs: list[str], run_id: str,
                   branch: str) -> dict:
    element = {"1H": "H", "13C": "C"}.get(nucleus, "H")
    tol = float(params.get(f"tol_{nucleus}_ppm", 0.15))
    tol_c = float(params.get("tol_13C_ppm", 1.5))
    tol_h = float(params.get("tol_1H_ppm", 0.15))
    bias = float(params.get(f"bias_{nucleus}_ppm", 0.0) or 0.0)
    bias_h = float(params.get("bias_1H_ppm", 0.0) or 0.0)
    bias_c = float(params.get("bias_13C_ppm", 0.0) or 0.0)
    w = {"COSY": float(params.get("w_COSY", 0.2)), "HSQC": float(params.get("w_HSQC", 0.5)),
         "HMBC": float(params.get("w_HMBC", 0.3))}
    cutoff = float(params.get("cutoff", 0.05))

    atom_map = {a["atom_id"]: a for a in atoms}
    cand_atoms = [a for a in atoms if a["element"] == element]
    adj = _adjacency(bonds)
    peak_map = {p["peak_id"]: p for p in peaks}

    obs = np.array([p["shift_ppm"] if p["shift_ppm"] is not None else np.nan
                    for p in peaks], dtype=float)
    pred = np.array([a["pred_ppm"] if a["pred_ppm"] is not None else np.nan
                     for a in cand_atoms], dtype=float) + bias

    # vectorized shift scores: shape (n_peaks, n_atoms)
    with np.errstate(invalid="ignore"):
        z = (obs[:, None] - pred[None, :]) / tol
        shift_scores = np.exp(-0.5 * z * z)
    shift_scores[np.isnan(z)] = np.nan

    def h_pred_on(carbon_id: str) -> list[float]:
        out = []
        for atom in atom_map.values():
            if (atom["element"] == "H" and atom.get("attaches") == carbon_id
                    and atom["pred_ppm"] is not None):
                out.append(atom["pred_ppm"] + bias_h)
        return out

    def edge_bonus(peak: dict, atom: dict) -> tuple[float, list[dict]]:
        bonus, evidence = 0.0, []
        pid = peak["peak_id"]
        for e in edges:
            if e["a"] != pid and e["b"] != pid:
                continue
            other = e["b"] if e["a"] == pid else e["a"]
            opeak = peak_map.get(other)
            if opeak is None or opeak["shift_ppm"] is None:
                continue
            kind = e["kind"]
            hit = None
            if nucleus == "1H":
                host = atom.get("attaches")
                if host is None:
                    continue
                if kind == "COSY":
                    for nb in adj.get(host, set()):
                        for hp in h_pred_on(nb):
                            if abs(opeak["shift_ppm"] - hp) <= tol_h:
                                hit = ("COSY", other, hp)
                elif kind == "HSQC":
                    cpred = atom_map.get(host, {}).get("pred_ppm")
                    if cpred is not None and abs(opeak["shift_ppm"] - (cpred + bias_c)) <= tol_c:
                        hit = ("HSQC", other, cpred + bias_c)
                elif kind == "HMBC":
                    for c2 in _within(adj, host, 2, 3):
                        cpred = atom_map.get(c2, {}).get("pred_ppm")
                        if cpred is not None and abs(opeak["shift_ppm"] - (cpred + bias_c)) <= tol_c:
                            hit = ("HMBC", other, cpred + bias_c)
            else:  # 13C
                if kind == "HSQC":
                    for hp in h_pred_on(atom["atom_id"]):
                        if abs(opeak["shift_ppm"] - hp) <= tol_h:
                            hit = ("HSQC", other, hp)
                elif kind == "HMBC":
                    for c2 in _within(adj, atom["atom_id"], 2, 3):
                        for hp in h_pred_on(c2):
                            if abs(opeak["shift_ppm"] - hp) <= tol_h:
                                hit = ("HMBC", other, hp)
            if hit:
                bonus += w.get(kind, 0.0)
                evidence.append({"edge": kind, "via": other, "matched": hit[2]})
        return bonus, evidence

    unknown_evidence: dict[str, list[dict]] = {}
    for m in missing:
        unknown_evidence.setdefault(m["a"], []).append(
            {"missing_edge": f"{m['kind']}:{m['a']}:{m['b']}"})

    candidates = []
    idx = 0
    for pi, peak in enumerate(peaks):
        scored = []
        for ai, atom in enumerate(cand_atoms):
            ss = shift_scores[pi, ai]
            notes = []
            if peak.get("overlap"):
                notes.append(f"overlap_group:{peak['overlap']}:not_uniquely_matchable")
            if np.isnan(ss):
                scored.append({"atom": atom, "score": None, "evidence": [],
                               "notes": notes + ["unconvertible_shift:kept_as_unknown"]})
                continue
            bonus, evidence = edge_bonus(peak, atom)
            unk = unknown_evidence.get(peak["peak_id"])
            if unk:
                evidence = evidence + unk
                notes.append("missing_edge:evidence_unknown_not_rejected")
            scored.append({"atom": atom, "score": float(ss) * (1.0 + bonus),
                           "evidence": evidence, "notes": notes})
        known = [s for s in scored if s["score"] is not None]
        best = max((s["score"] for s in known), default=None)
        kept = [s for s in scored if s["score"] is None
                or (best is not None and s["score"] >= max(cutoff, 0.2 * best))]
        kept.sort(key=lambda s: (s["score"] is None, -(s["score"] or 0.0),
                                 s["atom"]["atom_id"]))
        # ranks with ties
        rank, prev = 0, None
        tie_group = 0
        for s in kept:
            if s["score"] is None:
                s["rank"] = None
                continue
            if prev is None or abs(s["score"] - prev) > TIE_EPS:
                rank += 1
                tie_group += 1
            s["rank"] = rank
            s["tie"] = f"{run_id}-t{tie_group}"
            prev = s["score"]
        tied_best = [s for s in kept if s["rank"] == 1]
        for s in kept:
            idx += 1
            notes = list(s["notes"])
            if len(tied_best) > 1 and s["rank"] == 1:
                notes.append("tied_best:all_legal_candidates_retained")
            candidates.append({
                "cand_id": f"{run_id}-c{idx:03d}",
                "peak_id": peak["peak_id"], "atom_id": s["atom"]["atom_id"],
                "score": s["score"], "rank": s["rank"],
                "tie_group": s.get("tie"), "evidence": s["evidence"],
                "notes": notes,
                "status": "unknown" if s["score"] is None else "current",
                "src_docs": list(src_docs),
            })

    # atoms of this element with no plausible peak -> unknown, never forced
    unmatched = []
    for ai, atom in enumerate(cand_atoms):
        col = shift_scores[:, ai] if len(peaks) else np.array([])
        if len(col) == 0 or not np.any(np.nan_to_num(col, nan=0.0) >= 0.5):
            unmatched.append({"atom_id": atom["atom_id"],
                              "reason": "no_matching_peak:kept_unassigned"})
    return {"run_id": run_id, "branch": branch, "nucleus": nucleus,
            "algo": ALGO_VERSION, "candidates": candidates, "unmatched": unmatched}
