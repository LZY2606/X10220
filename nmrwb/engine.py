"""Domain engine: assignment candidates with evidence, not forced answers.

Guarantees exercised by the acceptance suite:

* overlapping peaks and missing peaks are never forced into a unique match;
* symmetry-equivalent nuclei share a class and remain co-eligible;
* COSY / HSQC / HMBC rows contribute *evidence* only (no veto);
* a missing correlation edge is weak negative evidence, never a hard reject;
* a coherent global chemical-shift offset is measured and reported as bias;
* tied best candidates are all retained with ``tie_with``;
* every rejected candidate carries a structured reason.

The engine is pure: it takes parsed inputs + parameters and returns a result.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .parser import ParsedSource, Peak, Edge, Atom

DEFAULT_PARAMS = {
    "algo_version": "assign-1.0.0",
    "shift_window_ppm": 0.5,
    "w_shift": 1.0,
    "w_integral": 0.4,
    "w_evidence": 0.6,
    "w_missing_edge": 0.2,
    "bias_threshold_ppm": 0.08,
    "overlap_tolerance_ppm": 0.03,
    "tie_eps": 0.02,
}

# A tiny constant to keep exp() finite.
EPS = 1e-9


@dataclass
class Evidence:
    code: str
    weight: float
    message: str
    source_ref: str
    supports: bool

    def to_dict(self) -> dict:
        return {"code": self.code, "weight": round(self.weight, 6),
                "message": self.message, "source_ref": self.source_ref,
                "supports": self.supports}


@dataclass
class Rejection:
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class Candidate:
    cand_id: str
    nucleus: str
    isotope: str
    symmetry_class: str
    peak_id: str
    peak_pid: str
    shift_ppm: Optional[float]
    expected_ppm: Optional[float]
    shift_delta: Optional[float]
    integral_delta: Optional[float]
    score: float
    rank: int
    tie_with: List[str] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)
    rejected: bool = False
    rejection: Optional[Rejection] = None
    depends_on: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.cand_id,
            "nucleus": self.nucleus,
            "isotope": self.isotope,
            "symmetry_class": self.symmetry_class,
            "peak_id": self.peak_id,
            "peak_pid": self.peak_pid,
            "shift_ppm": self.shift_ppm,
            "expected_ppm": self.expected_ppm,
            "shift_delta": self.shift_delta,
            "integral_delta": self.integral_delta,
            "score": (None if self.score != self.score else round(self.score, 6)),
            "rank": self.rank,
            "tie_with": self.tie_with,
            "evidence": [e.to_dict() for e in self.evidence],
            "rejected": self.rejected,
            "rejection": self.rejection.to_dict() if self.rejection else None,
            "depends_on": self.depends_on,
            "status": "rejected" if self.rejected else "eligible",
        }


@dataclass
class NucleusResult:
    nucleus: str
    isotope: str
    symmetry_class: str
    equiv_size: int
    candidates: List[Candidate] = field(default_factory=list)
    state: str = "unknown"          # unknown | covered | missing | unique
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return {"nucleus": self.nucleus, "isotope": self.isotope,
                "symmetry_class": self.symmetry_class, "equiv_size": self.equiv_size,
                "state": self.state, "note": self.note,
                "candidates": [c.to_dict() for c in self.candidates]}


@dataclass
class PeakGroup:
    group_id: str
    isotope: str
    peak_ids: List[str]
    spread_ppm: float
    note: str

    def to_dict(self) -> dict:
        return {"id": self.group_id, "isotope": self.isotope,
                "peak_ids": self.peak_ids, "spread_ppm": self.spread_ppm,
                "note": self.note}


@dataclass
class RunResult:
    algo_version: str
    param_fingerprint: str
    inputs_fingerprint: str
    params: Dict
    nuclei: List[NucleusResult]
    peak_groups: List[PeakGroup]
    unmatched_peaks: List[dict]
    global_bias: dict
    statistics: dict
    judgements: List[dict]
    source_refs: Dict[str, str]

    def to_dict(self) -> dict:
        return {
            "algo_version": self.algo_version,
            "param_fingerprint": self.param_fingerprint,
            "inputs_fingerprint": self.inputs_fingerprint,
            "params": self.params,
            "nuclei": [n.to_dict() for n in self.nuclei],
            "peak_groups": [g.to_dict() for g in self.peak_groups],
            "unmatched_peaks": self.unmatched_peaks,
            "global_bias": self.global_bias,
            "statistics": self.statistics,
            "judgements": self.judgements,
            "source_refs": self.source_refs,
        }


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------

@dataclass
class EngineInputs:
    isotope_sources: Dict[str, ParsedSource]   # isotope ("1H"/"13C") -> 1D source
    edges: Dict[str, List[Edge]]               # experiment -> edges
    skeleton: Optional[ParsedSource]
    rules: Optional[ParsedSource]
    refs: Dict[str, str]                       # kind -> "<sid>/<vid>"


def assemble_inputs(sources: Dict[str, Tuple[str, ParsedSource]]) -> EngineInputs:
    """``sources`` maps source_id -> (version_id, ParsedSource)."""
    isotope_sources: Dict[str, ParsedSource] = {}
    edges: Dict[str, List[Edge]] = {"cosy": [], "hsqc": [], "hmbc": []}
    skeleton = rules = None
    refs: Dict[str, str] = {}
    for sid, (vid, ps) in sources.items():
        ref = f"{sid}/{vid}"
        if ps.kind in ("1h", "13c"):
            isotope = "1H" if ps.kind == "1h" else "13C"
            isotope_sources[isotope] = ps
            refs[isotope] = ref
        elif ps.kind in edges:
            edges[ps.kind].extend(ps.edges)
            refs.setdefault(ps.kind, ref)
        elif ps.kind == "skeleton":
            skeleton = ps
            refs["skeleton"] = ref
        elif ps.kind == "rules":
            rules = ps
            refs["rules"] = ref
    return EngineInputs(isotope_sources, edges, skeleton, rules, refs)


def _params(rules: Optional[ParsedSource]) -> Dict:
    params = dict(DEFAULT_PARAMS)
    if rules is not None:
        for k, v in rules.params.items():
            params[k] = v
    return params


def fingerprint(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def run_signature(source_versions: Dict[str, str], params: Dict) -> str:
    return fingerprint({"sv": dict(sorted(source_versions.items())),
                        "pf": _param_fp(params)})


def _param_fp(params: Dict) -> str:
    keys = ("algo_version", "shift_window_ppm", "w_shift", "w_integral",
            "w_evidence", "w_missing_edge", "bias_threshold_ppm",
            "overlap_tolerance_ppm", "tie_eps",
            "tie_break_use_integral", "tie_break_use_evidence")
    return fingerprint({k: params.get(k) for k in keys})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _peak_ref(edge_dim: str) -> Tuple[str, str]:
    """Edge dims are written ``1H:p1`` / ``13C:c2`` -> (isotope, pid)."""
    iso, _, pid = edge_dim.partition(":")
    return iso, pid


def _peak_index(ins: EngineInputs) -> Dict[Tuple[str, str], Peak]:
    idx = {}
    for isotope, ps in ins.isotope_sources.items():
        for p in ps.peaks:
            idx[(isotope, p.pid)] = p
    return idx


def _priors_by_isotope(ins: EngineInputs) -> Dict[str, Dict[str, Tuple[float, float]]]:
    out: Dict[str, Dict[str, Tuple[float, float]]] = {}
    if ins.rules:
        for pr in ins.rules.priors:
            if pr.expected_ppm.value is None or pr.tolerance_ppm.value is None:
                continue
            out.setdefault(pr.isotope, {})[pr.label] = (
                pr.expected_ppm.value, pr.tolerance_ppm.value)
    return out


def _atoms_by_isotope(ins: EngineInputs) -> Dict[str, List[Atom]]:
    out: Dict[str, List[Atom]] = {}
    if ins.skeleton:
        for a in ins.skeleton.atoms:
            out.setdefault(a.isotope, []).append(a)
    return out


def _class_members(ins: EngineInputs) -> Dict[str, List[str]]:
    classes: Dict[str, List[str]] = {}
    if ins.skeleton:
        for a in ins.skeleton.atoms:
            classes.setdefault(a.symmetry_class, []).append(a.label)
    return classes


def _build_edge_lookups(ins: EngineInputs):
    """Return direct-neighbor maps keyed by (isotope, peak pid).

    cosy/hsqc map 1H<->1H or 1H<->13C; hmbc is long range.  Each entry is
    (other_isotope, other_pid, experiment, source_ref, intensity).
    """
    direct: Dict[Tuple[str, str], List] = {}
    long_range: Dict[Tuple[str, str], List] = []
    ref_by_kind = ins.refs
    for kind in ("cosy", "hsqc", "hmbc"):
        for e in ins.edges.get(kind, []):
            iso1, p1 = _peak_ref(e.dim1)
            iso2, p2 = _peak_ref(e.dim2)
            src = ref_by_kind.get(kind, "")
            triple1 = (iso2, p2, kind, src, e.intensity.value)
            triple2 = (iso1, p1, kind, src, e.intensity.value)
            direct.setdefault((iso1, p1), []).append(triple1)
            direct.setdefault((iso2, p2), []).append(triple2)
            if kind == "hmbc":
                long_range.append(((iso1, p1), (iso2, p2), src, e.eid))
    return direct, long_range


def _bond_pairs(ins: EngineInputs) -> List[Tuple[str, str]]:
    if not ins.skeleton:
        return []
    return [(b.a, b.b) for b in ins.skeleton.bonds] + \
           [(b.b, b.a) for b in ins.skeleton.bonds]


def _bond_distance(ins: EngineInputs) -> Dict[Tuple[str, str], int]:
    """Shortest bond distance between atom labels (ignores H-H bonds)."""
    adj: Dict[str, set] = {}
    if ins.skeleton:
        for b in ins.skeleton.bonds:
            adj.setdefault(b.a, set()).add(b.b)
            adj.setdefault(b.b, set()).add(b.a)
    dist: Dict[Tuple[str, str], int] = {}
    labels = list(adj)
    for src in labels:
        dist[(src, src)] = 0
        frontier = [src]
        seen = {src}
        d = 0
        while frontier:
            d += 1
            nxt = []
            for node in frontier:
                for nb in adj.get(node, ()):
                    if nb not in seen:
                        seen.add(nb)
                        dist[(src, nb)] = d
                        nxt.append(nb)
            frontier = nxt
    return dist


def _find_overlap_groups(peaks: List[Peak], tol: float, isotope: str,
                         source_ref: str) -> Tuple[List[PeakGroup], Dict[str, str]]:
    usable = [p for p in peaks if p.shift.value is not None]
    usable.sort(key=lambda p: (p.shift.value, p.pid))
    groups: List[PeakGroup] = []
    member_of: Dict[str, str] = {}
    cluster: List[Peak] = []

    def flush(cl: List[Peak]):
        if len(cl) >= 2:
            spread = max(p.shift.value for p in cl) - min(p.shift.value for p in cl)
            gid = f"G:{isotope}:" + "+".join(p.pid for p in cl)
            groups.append(PeakGroup(gid, isotope,
                                    [f"{source_ref}/P:{p.pid}" for p in cl],
                                    round(spread, 6),
                                    "unresolved overlap; never forced unique"))
            for p in cl:
                member_of[p.pid] = gid

    for p in usable:
        if cluster and p.shift.value - cluster[-1].shift.value <= tol + EPS:
            cluster.append(p)
        else:
            flush(cluster)
            cluster = [p]
    flush(cluster)
    return groups, member_of


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _global_bias(nuclei_rows: List[Tuple[Atom, Peak, float]],
                 threshold: float) -> dict:
    """Median signed (peak - prior) residual across currently paired rows.

    A coherent offset over the threshold is reported as bias.  It is measured,
    not silently corrected, so historical runs remain interpretable.
    """
    residuals = [peak.shift.value - expected
                 for _, peak, expected in nuclei_rows
                 if peak.shift.value is not None]
    if not residuals:
        return {"detected": False, "offset_ppm": None, "method": "median-residual",
                "n": 0, "threshold_ppm": threshold}
    residuals.sort()
    mid = len(residuals) // 2
    med = residuals[mid] if len(residuals) % 2 else \
        (residuals[mid - 1] + residuals[mid]) / 2.0
    return {"detected": abs(med) >= threshold, "offset_ppm": round(med, 6),
            "method": "median-residual", "n": len(residuals),
            "threshold_ppm": threshold}


def _nucleus_bond_neighbors(ins: EngineInputs, label: str) -> Tuple[set, set]:
    """Return (directly bonded labels, 2-bond labels) for HMBC semantics."""
    dist = _bond_distance(ins)
    one = {b for (a, b), d in dist.items() if a == label and d == 1}
    two = {b for (a, b), d in dist.items() if a == label and d == 2}
    return one, two


def compute(sources: Dict[str, Tuple[str, ParsedSource]],
            extra_params: Optional[Dict] = None) -> RunResult:
    ins = assemble_inputs(sources)
    params = _params(ins.rules)
    if extra_params:
        params.update(extra_params)
    pf = _param_fp(params)

    judgements: List[dict] = []
    peak_index = _peak_index(ins)
    priors = _priors_by_isotope(ins)
    atoms_by_iso = _atoms_by_isotope(ins)
    direct_edges, _lr = _build_edge_lookups(ins)

    overlap_by_iso: Dict[str, List[PeakGroup]] = {}
    member_of_all: Dict[Tuple[str, str], str] = {}
    for isotope, ps in ins.isotope_sources.items():
        groups, member_of = _find_overlap_groups(
            ps.peaks, params["overlap_tolerance_ppm"], isotope, ins.refs[isotope])
        overlap_by_iso[isotope] = groups
        for pid, gid in member_of.items():
            member_of_all[(isotope, pid)] = gid
        for g in groups:
            judgements.append({
                "code": "OVERLAP_NOT_FORCED",
                "severity": "info",
                "object": g.group_id,
                "message": ("overlapping peaks retained as separate candidates; "
                            "no unique match is forced"),
                "basis": {"spread_ppm": g.spread_ppm,
                          "tolerance_ppm": params["overlap_tolerance_ppm"]},
            })

    window = float(params["shift_window_ppm"])
    tie_eps = float(params["tie_eps"])

    # First pass: nearest peak per nucleus for bias statistics.  Bias is
    # estimated per isotope so an offset on one dimension is not diluted by
    # zero-residual nuclei on another.
    bias_rows_by_iso: Dict[str, List[Tuple[Atom, Peak, float]]] = {}
    for isotope, atoms in atoms_by_iso.items():
        ps = ins.isotope_sources.get(isotope)
        peaks = ps.peaks if ps else []
        for atom in atoms:
            prior = priors.get(isotope, {}).get(atom.label)
            if not prior:
                continue
            expected, _tol = prior
            best = None
            for pk in peaks:
                if pk.shift.value is None:
                    continue
                if abs(pk.shift.value - expected) <= window:
                    if best is None or abs(pk.shift.value - expected) < \
                            abs(best.shift.value - expected):
                        best = pk
            if best is not None:
                bias_rows_by_iso.setdefault(isotope, []).append(
                    (atom, best, expected))

    bias_by_iso = {iso: _global_bias(rows, float(params["bias_threshold_ppm"]))
                   for iso, rows in bias_rows_by_iso.items()}
    detected_iso = {iso: b for iso, b in bias_by_iso.items() if b["detected"]}
    bias = {"detected": bool(detected_iso),
            "by_isotope": bias_by_iso,
            "offset_ppm": (next(iter(detected_iso.values()))["offset_ppm"]
                           if detected_iso else None),
            "method": "median-residual-per-isotope",
            "threshold_ppm": float(params["bias_threshold_ppm"])}
    for iso, b in detected_iso.items():
        judgements.append({
            "code": "GLOBAL_SHIFT_BIAS",
            "severity": "warning",
            "object": f"run:{iso}",
            "message": ("coherent shift offset detected on one dimension; "
                        "candidates are scored against priors and flagged, "
                        "not silently re-referenced"),
            "basis": {"isotope": iso, **b},
        })

    used_peak_ids: Dict[Tuple[str, str], List[str]] = {}
    nuclei_results: List[NucleusResult] = []
    tie_integral_flag = bool(params.get("tie_break_use_integral"))
    tie_evidence_flag = bool(params.get("tie_break_use_evidence"))

    for isotope, atoms in atoms_by_iso.items():
        ps = ins.isotope_sources.get(isotope)
        peaks = ps.peaks if ps else []
        src_ref = ins.refs.get(isotope, "")
        for atom in atoms:
            nr = NucleusResult(atom.label, isotope, atom.symmetry_class,
                               atom.equiv_size)
            prior = priors.get(isotope, {}).get(atom.label)
            cands: List[Candidate] = []
            if prior is None:
                nr.state = "unknown"
                nr.note = "no prior for nucleus in rules source"
                judgements.append({"code": "NO_PRIOR", "severity": "warning",
                                   "object": atom.label,
                                   "message": nr.note, "basis": {"rules": ins.refs.get("rules")}})
                nuclei_results.append(nr)
                continue
            expected, tol = prior
            in_window = False
            for pk in peaks:
                peak_full = f"{src_ref}/P:{pk.pid}"
                deps = [src_ref, ins.refs.get("rules", ""), ins.refs.get("skeleton", "")]
                if pk.shift.value is None:
                    cands.append(Candidate(
                        _cid(atom.label, pk.pid, pf), atom.label, isotope,
                        atom.symmetry_class, peak_full, pk.pid, None, expected,
                        None, None, float("nan"), 0,
                        rejected=True,
                        rejection=Rejection("UNCONVERTIBLE_SHIFT",
                                            "peak shift has no numeric value; kept at source"),
                        depends_on=[d for d in deps if d]))
                    continue
                delta = pk.shift.value - expected
                if abs(delta) > window:
                    cands.append(Candidate(
                        _cid(atom.label, pk.pid, pf), atom.label, isotope,
                        atom.symmetry_class, peak_full, pk.pid,
                        pk.shift.value, expected, round(delta, 6), None,
                        float("nan"), 0,
                        rejected=True,
                        rejection=Rejection(
                            "OUTSIDE_SHIFT_WINDOW",
                            f"|delta|={abs(delta):.3f} ppm > window {window} ppm"),
                        depends_on=[d for d in deps if d]))
                    continue
                in_window = True
                evid, miss_notes = _evidence_for(ins, atom, pk, isotope,
                                                 direct_edges, priors)
                score, parts = _score(pk, atom, expected, tol, delta, evid,
                                      miss_notes, params, bias)
                integral_delta = parts.get("integral_abs")
                c = Candidate(_cid(atom.label, pk.pid, pf), atom.label, isotope,
                              atom.symmetry_class, peak_full, pk.pid,
                              pk.shift.value, expected, round(delta, 6),
                              None if integral_delta is None else round(integral_delta, 6),
                              score, 0, evidence=evid,
                              depends_on=[d for d in deps if d])
                cands.append(c)
                used_peak_ids.setdefault((isotope, pk.pid), []).append(atom.label)

            # Missing edge diagnostics are weak evidence, added to surviving cands.
            for c in cands:
                if not c.rejected:
                    used_peak_ids.setdefault((isotope, c.peak_pid), []).append(atom.label)

            eligible = [c for c in cands if not c.rejected]
            rejected = [c for c in cands if c.rejected]
            eligible.sort(key=lambda c: (-c.score, c.peak_pid))
            # Dense ranks; scores within tie_eps share a rank.
            rank = 0
            last_score = None
            for i, c in enumerate(eligible):
                if last_score is None or abs(c.score - last_score) > tie_eps:
                    rank += 1
                c.rank = rank
                last_score = c.score

            missing_codes = sorted({e.code for c in eligible for e in c.evidence
                                    if e.code.startswith("MISSING_EXPECTED")})
            for code in missing_codes:
                judgements.append({
                    "code": code, "severity": "info",
                    "object": atom.label,
                    "message": ("expected correlation absent; recorded as weak "
                                "negative evidence only, never a veto"),
                    "basis": {"isotope": isotope,
                              "source_present": bool(ins.edges.get(
                                  "cosy" if code.endswith("COSY") else
                                  ("hsqc" if code.endswith("HSQC") else "hmbc")))}})

            if not eligible:
                nr.state = "missing"
                nr.note = ("no peak inside shift window; nucleus left unassigned "
                           "rather than force-matched")
                judgements.append({
                    "code": "MISSING_PEAK_NOT_FORCED", "severity": "warning",
                    "object": atom.label,
                    "message": nr.note,
                    "basis": {"window_ppm": window, "expected_ppm": expected}})
            else:
                _apply_ties(eligible, tie_eps, tie_integral_flag, tie_evidence_flag)
                top_rank = eligible[0].rank
                top_cluster = [c for c in eligible if c.rank == top_rank]
                nr.state = "unique" if len(top_cluster) == 1 and \
                    not top_cluster[0].tie_with else "covered"
                if nr.state == "covered":
                    top = [c for c in eligible if c.rank == 1]
                    if len(top) > 1 or top[0].tie_with:
                        nr.note = "tied/ambiguous; all legal candidates retained"
                        judgements.append({
                            "code": "TIED_BEST_CANDIDATES", "severity": "info",
                            "object": atom.label,
                            "message": nr.note,
                            "basis": {"candidates": [c.cand_id for c in top],
                                      "tie_eps": tie_eps}})
            nr.candidates = eligible + rejected
            nuclei_results.append(nr)

    # Peaks not eligible for any nucleus.
    unmatched: List[dict] = []
    for isotope, ps in ins.isotope_sources.items():
        for pk in ps.peaks:
            if (isotope, pk.pid) not in used_peak_ids:
                unmatched.append({"peak_id": f"{ins.refs[isotope]}/P:{pk.pid}",
                                  "shift_ppm": pk.shift.value,
                                  "reason": "no nucleus prior window covers this peak"})

    # Symmetry judgement.  Two representations are supported: several atoms
    # sharing one symmetry_class, or one environment node whose equiv_size>1.
    if ins.skeleton:
        emitted: set = set()
        for cls, members in _class_members(ins).items():
            iso = next((a.isotope for a in ins.skeleton.atoms
                        if a.label in members), None)
            same_iso = [a for a in ins.skeleton.atoms
                        if a.symmetry_class == cls and a.isotope == iso]
            total_size = sum(a.equiv_size for a in same_iso)
            if len(same_iso) > 1 or total_size > 1:
                if cls in emitted:
                    continue
                emitted.add(cls)
                judgements.append({
                    "code": "SYMMETRY_EQUIVALENCE_PRESERVED", "severity": "info",
                    "object": cls,
                    "message": ("symmetry-equivalent nuclei share priors and peaks; "
                                "they are not split into distinct forced assignments"),
                    "basis": {"isotope": iso, "members": [a.label for a in same_iso],
                              "equiv_size": total_size}})

    inputs_fp = fingerprint({
        "sources": {sid: vid for sid, (vid, _) in sources.items()},
        "kind": {sid: ps.kind for sid, (_, ps) in sources.items()},
        "peak_shifts": [
            [p.shift.raw for ps in [v[1] for v in sources.values()]
             for p in getattr(ps, "peaks", [])]
        ],
    })
    stats = {
        "n_nuclei": len(nuclei_results),
        "n_unique": sum(1 for n in nuclei_results if n.state == "unique"),
        "n_ambiguous": sum(1 for n in nuclei_results if n.state == "covered"),
        "n_missing": sum(1 for n in nuclei_results if n.state == "missing"),
        "n_overlap_groups": sum(len(g) for g in overlap_by_iso.values()),
        "n_unmatched_peaks": len(unmatched),
        "n_judgements": len(judgements),
    }
    return RunResult(params["algo_version"], pf, inputs_fp, params,
                     nuclei_results,
                     overlap_by_iso.get("1H", []) + overlap_by_iso.get("13C", []),
                     unmatched, bias, stats, judgements, ins.refs)


def _cid(nucleus: str, pid: str, pf: str) -> str:
    return f"C:{nucleus}->{pid}"


# ---------------------------------------------------------------------------
# Evidence + scoring
# ---------------------------------------------------------------------------

def _evidence_for(ins: EngineInputs, atom: Atom, peak: Peak, isotope: str,
                  direct_edges: Dict[Tuple[str, str], List],
                  priors: Dict[str, Dict[str, Tuple[float, float]]]
                  ) -> Tuple[List[Evidence], List[str]]:
    """Translate observed correlation edges into soft evidence.

    HSQC: this peak is bonded to a 13C peak whose prior matches a bonded carbon.
    COSY: this 1H peak is adjacent to another 1H peak matching a bonded neighbor.
    HMBC: long-range (2-3 bond) consistency.
    A *missing* expected edge only yields weak negative evidence.
    """
    evid: List[Evidence] = []
    missing: List[str] = []
    rows = direct_edges.get((isotope, peak.pid), [])
    one_bond, two_bond = _nucleus_bond_neighbors(ins, atom.label)

    for other_iso, other_pid, kind, src, intensity in rows:
        other_peak = _peak_lookup(ins, other_iso, other_pid)
        if other_peak is None or other_peak.shift.value is None:
            continue
        match_labels = _labels_near_prior(ins, priors, other_iso, other_peak)
        if kind == "hsqc":
            hits = one_bond & match_labels
            if hits:
                evid.append(Evidence("HSQC_ONE_BOND", 1.0,
                    f"HSQC links {peak.pid} to {other_pid}, consistent with bond "
                    f"{atom.label}-{sorted(hits)[0]}", src, True))
            else:
                evid.append(Evidence("HSQC_NO_BOND_MATCH", 0.15,
                    f"HSQC partner {other_pid} matches no carbon bonded to {atom.label}",
                    src, False))
        elif kind == "cosy":
            hits = one_bond & match_labels
            if hits:
                evid.append(Evidence("COSY_NEIGHBOR", 0.8,
                    f"COSY partner {other_pid} is consistent with neighbor "
                    f"{sorted(hits)[0]}", src, True))
            else:
                evid.append(Evidence("COSY_WEAK", 0.1,
                    f"COSY partner {other_pid} has no bonded-neighbor prior match",
                    src, False))
        elif kind == "hmbc":
            long_hits = (one_bond | two_bond) & match_labels
            if long_hits:
                evid.append(Evidence("HMBC_LONG_RANGE", 0.6,
                    f"HMBC partner {other_pid} consistent with 1-2 bond neighbor "
                    f"{sorted(long_hits)[0]}", src, True))
            else:
                evid.append(Evidence("HMBC_WEAK", 0.05,
                    f"HMBC partner {other_pid} not near any 1-2 bond neighbor prior",
                    src, False))

    # Missing-edge weak negatives (expectation derived from the skeleton only).
    if ins.skeleton is not None:
        observed_kinds = {(iso, pid, kind)
                          for iso, pid, kind, _s, _i in rows}
        if isotope == "1H" and one_bond:
            has_cosy = any(k == "cosy" for _i, _p, k, _s, _x in rows)
            if _has_any_edge(ins, "cosy") and not has_cosy:
                msg = (f"no COSY edge incident to {peak.pid} although skeleton "
                       f"predicts neighbor(s) {sorted(one_bond)}")
                evid.append(Evidence("MISSING_EXPECTED_COSY",
                                     -0.15, msg, ins.refs.get("cosy", ""), False))
                missing.append("MISSING_EXPECTED_COSY")
        if isotope == "1H" and one_bond:
            has_hsqc = any(k == "hsqc" for _i, _p, k, _s, _x in rows)
            if _has_any_edge(ins, "hsqc") and not has_hsqc:
                evid.append(Evidence("MISSING_EXPECTED_HSQC", -0.2,
                    f"no HSQC edge incident to {peak.pid} despite bonded heavy atom",
                    ins.refs.get("hmbc", ""), False))
                missing.append("MISSING_EXPECTED_HSQC")
    return evid, missing


def _has_any_edge(ins: EngineInputs, kind: str) -> bool:
    return bool(ins.edges.get(kind))


def _peak_lookup(ins: EngineInputs, isotope: str, pid: str):
    ps = ins.isotope_sources.get(isotope)
    if not ps:
        return None
    for p in ps.peaks:
        if p.pid == pid:
            return p
    return None


def _labels_near_prior(ins: EngineInputs,
                       priors: Dict[str, Dict[str, Tuple[float, float]]],
                       isotope: str, peak: Peak,
                       window: Optional[float] = None) -> set:
    if peak.shift.value is None:
        return set()
    w = window if window is not None else 0.4
    labels = set()
    for label, (expected, tol) in priors.get(isotope, {}).items():
        if abs(peak.shift.value - expected) <= max(w, tol):
            labels.add(label)
    return labels


def _score(peak: Peak, atom: Atom, expected: float, tol: float,
           delta: float, evid: List[Evidence], missing: List[str],
           params: Dict, bias: dict):
    w_shift = float(params["w_shift"])
    w_int = float(params["w_integral"])
    w_ev = float(params["w_evidence"])
    w_miss = float(params["w_missing_edge"])

    # Shift component: Gaussian on tolerance (a unit-bearing ppm distance).
    sigma = max(tol, 1e-6)
    shift_term = math.exp(- (delta ** 2) / (2 * sigma ** 2))

    # Integral component: expected integral equals equivalence class size.
    int_term = 1.0
    integral_abs = None
    if peak.integral.value is not None:
        expected_int = float(atom.equiv_size)
        integral_abs = abs(peak.integral.value - expected_int)
        int_term = math.exp(- (integral_abs ** 2) / 0.5)

    ev_term = sum(max(e.weight, 0.0) for e in evid)
    neg_ev = sum(-min(e.weight, 0.0) for e in evid)
    ev_component = ev_term / (1.0 + ev_term) - w_miss * min(neg_ev, 1.0)

    score = w_shift * shift_term + w_int * int_term + w_ev * ev_component
    parts = {"shift": shift_term, "integral": int_term,
             "integral_abs": integral_abs, "evidence": ev_component}
    return score, parts


def _apply_ties(eligible: List[Candidate], tie_eps: float,
                use_integral: bool, use_evidence: bool) -> None:
    """Mark ties among the top ranked candidates.

    By default nothing breaks a tie: close scores share rank 1.  Optional
    deterministic tie-breakers (parameter revision v2) may distinguish them.
    """
    if not eligible:
        return
    top = eligible[0].score
    tied = [c for c in eligible if abs(c.score - top) <= tie_eps]
    if len(tied) <= 1:
        for c in tied:
            c.tie_with = []
        return
    # Optional, explicit tie-breakers operate on first-class observables.
    if use_integral or use_evidence:
        def key(c: Candidate):
            int_bonus = 0.0
            if use_integral and c.integral_delta is not None:
                int_bonus = -c.integral_delta
            ev_bonus = 0.0
            if use_evidence:
                ev_bonus = sum(e.weight for e in c.evidence if e.supports)
            return (int_bonus + ev_bonus, -c.shift_delta if c.shift_delta is not None else 0)
        tied_sorted = sorted(tied, key=lambda c: (-key(c)[0], key(c)[1], c.peak_pid))
        best_key = key(tied_sorted[0])
        winners = [c for c in tied_sorted if key(c) == best_key]
        if len(winners) == 1:
            winners[0].tie_with = []
            return
    for c in tied:
        c.tie_with = sorted(o.cand_id for o in tied if o is not c)
