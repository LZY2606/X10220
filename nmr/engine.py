"""Assignment-candidate engine.

It never forces a unique answer: every nucleus group gets all legal
candidates; rejected peaks keep their rejection reasons; a missing peak
keeps an ``unknown`` candidate. The coupling network is evidence only:
expected-but-unobserved edges produce evidence gaps, never an automatic
rejection. Deterministic ordering and tie handling use a fixed recorded seed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from . import ALGORITHM_VERSION
from .units import canonical

DEFAULT_PARAMS = {
    "h_tolerance": 0.15,
    "c_tolerance": 0.8,
    "h_overlap": 0.02,
    "c_overlap": 0.15,
    "h_edge_tol": 0.02,
    "c_edge_tol": 0.5,
    "w_shift": 0.55,
    "w_mult": 0.10,
    "w_integral": 0.10,
    "w_edges": 0.25,
    "integral_ratio": 1.5,
    "h_bias_threshold": 0.03,
    "c_bias_threshold": 0.3,
    "tie_epsilon": 0.005,
    "seed": 20260923,
}


def fingerprint_params(params: Dict[str, Any]) -> str:
    body = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(("params:" + body).encode("utf-8")).hexdigest()[:16]


@dataclass
class Group:
    gid: str
    channel: str
    sites: List[str]
    expected: float
    multiplicity: Optional[str]
    integral: Optional[float]
    attached: Dict[str, str]


@dataclass
class Candidate:
    group: str
    peak: Optional[str]
    status: str
    score: Optional[float]
    terms: Dict[str, Any]
    rejection_reasons: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    symmetric_with: List[str] = field(default_factory=list)
    depends_on: List[List[Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "peak": self.peak,
            "status": self.status,
            "score": self.score,
            "terms": self.terms,
            "rejection_reasons": self.rejection_reasons,
            "flags": self.flags,
            "symmetric_with": self.symmetric_with,
            "depends_on": self.depends_on,
            "uid": self.group + "->" + (self.peak if self.peak else "unknown"),
        }


def _mult_key(m: Optional[str]) -> Optional[str]:
    if not m:
        return None
    return m.lower().replace("-", "").replace(" ", "")


def _integral_term(observed, expected, ratio):
    if observed is None or expected is None:
        return 0.5, "unknown"
    lo, hi = expected / ratio, expected * ratio
    if lo <= observed <= hi:
        return 1.0, "compatible"
    return 0.0, "incompatible(obs=%s,exp=%s)" % (observed, expected)


def build_groups(doc) -> List[Group]:
    groups: Dict[str, Group] = {}
    for site in doc.sites:
        if site.element not in ("H", "C"):
            continue
        exp = canonical(site.expected) if site.expected is not None else None
        if exp is None:
            continue
        gid = site.group if site.group else ("grp:" + site.id)
        integ = canonical(site.integral)
        g = groups.setdefault(
            gid,
            Group(gid, "H" if site.element == "H" else "C", [], exp,
                  _mult_key(site.multiplicity), integ, {}),
        )
        g.sites.append(site.id)
        if site.attached:
            g.attached[site.id] = site.attached
    return sorted(groups.values(), key=lambda g: g.gid)


def _peak_values(spec):
    out = {}
    if spec:
        for p in spec.peaks:
            val = canonical(p.shift)
            if val is not None:
                out[p.id] = val
    return out


def _overlap_clusters(peak_vals, threshold):
    ordered = sorted(peak_vals.items(), key=lambda kv: (kv[1], kv[0]))
    clusters: List[List[str]] = []
    for pid, val in ordered:
        if clusters and abs(val - peak_vals[clusters[-1][-1]]) <= threshold:
            clusters[-1].append(pid)
        else:
            clusters.append([pid])
    return [c for c in clusters if len(c) > 1]


def rank_candidates(cands: List[Candidate], eps: float, tie_seed: int) -> List[Candidate]:
    legal = [c for c in cands if c.status == "legal"]
    other = [c for c in cands if c.status != "legal"]
    # deterministic, seed-jittered micro-order so equal scores remain ties but
    # ties get a stable, reproducible resolution order recorded in the run
    rng = np.random.default_rng(tie_seed)
    jitter = {id(c): float(rng.uniform(0, 1e-6)) for c in legal}
    legal.sort(key=lambda c: (-c.score, c.peak, jitter[id(c)]))
    other.sort(key=lambda c: (c.peak or "~", c.status))
    return legal + other


def summarize_ambiguity(ranked: List[Candidate], eps: float) -> Dict[str, Any]:
    legal = [c for c in ranked if c.status == "legal"]
    if not legal:
        return {"kind": "unknown", "peaks": [], "message": "no legal candidate"}
    top = legal[0].score
    tied = [c for c in legal if abs(c.score - top) <= eps]
    overlap = sorted({p for c in legal for f in c.flags if f.startswith("overlap_cluster:")
                      for p in f.split(":", 1)[1].split(",") if c.peak and p in f})
    legal_overlap = [p for p in {c.peak for c in legal}
                     if any(p in c.flags and f.startswith("overlap") for c in legal for f in c.flags)]
    flags = set()
    if len(tied) > 1:
        flags.add("tied_optimal")
    if any(any(f.startswith("overlap_cluster:") for f in c.flags) for c in legal):
        flags.add("overlapping_peaks")
    kind = "unique"
    if len(tied) > 1:
        kind = "tie"
    elif any(any(f.startswith("overlap_cluster:") for f in c.flags) for c in legal):
        kind = "overlap"
    return {
        "kind": kind,
        "flags": sorted(flags),
        "tied_peaks": [c.peak for c in tied],
        "all_legal_peaks": [c.peak for c in legal],
        "message": {
            "unique": "single best candidate",
            "tie": "%d optimal candidates share the top score; all retained" % len(tied),
            "overlap": "candidate peaks overlap; not forced unique",
        }[kind],
    }


def summarize_gaps(ranked: List[Candidate]) -> List[str]:
    gaps = set()
    for c in ranked:
        if c.status == "legal":
            for state in str(c.terms.get("edges", "")).split("|"):
                if state.endswith(":evidence_gap"):
                    gaps.add(state)
    return sorted(gaps)
