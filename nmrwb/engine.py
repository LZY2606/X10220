"""归属候选引擎（engine-1.0.0）。

关键领域规则（作为机器可复核判定，而不是唯一答案）：
- 重叠峰：阈值内峰组成重叠簇；簇内不强制唯一匹配，产出歧义组。
- 对称原子：equivalence>1 的核与积分>1 的峰彼此等价，保留全部并列候选。
- 缺失相关边：作为证据惩罚与 finding，绝不据此伪造边或强制结论。
- 化学位移整体偏置：仅在 bias_correction 开启时按核种类做整体平移估计，
  并给出 bias_suspected 诊断；关闭时整体超差候选被拒绝而非强行归属。
- 并列最优：所有并列最优候选都保留，rank 相同；同分序由稳定键决定。
- 耦合网络（COSY/HSQC/HMBC）只用作证据，不用于强行唯一化。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import ENGINE_VERSION
from .ident import digest
from .model import Atom, Dataset, Edge, Peak
from .params import normalize_params, params_fingerprint


def _r6(x: Optional[float]) -> Optional[float]:
    return round(float(x), 6) if isinstance(x, (int, float)) else None


@dataclass
class Candidate:
    id: str
    nucleus: str
    kind: str
    peak_id: str
    peak_ref: str
    status: str            # candidate | rejected | unknown
    score: Optional[float]
    rank: Optional[int]
    raw_residual_ppm: Optional[float]
    corrected_residual_ppm: Optional[float]
    multiplicity_match: Optional[bool]
    multiplicity: Optional[str]
    atom_multiplicity: Optional[str]
    evidence: Dict[str, Any]
    reject_reasons: List[str]
    symmetry: Dict[str, Any]
    overlap_group: Optional[str]
    direct_sources: List[str]
    algorithm_version: str
    params_fingerprint: str

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Snapshot:
    id: str
    inputs_hash: str
    algorithm_version: str
    params_fingerprint: str
    created_at: str
    bias: Dict[str, Any]
    overlap_groups: List[Dict[str, Any]]
    symmetry_groups: List[Dict[str, Any]]
    candidates: List[Candidate]
    ambiguity_groups: List[Dict[str, Any]]
    findings: List[Dict[str, Any]]
    direct_sources: List[str]
    context_fingerprint: str
    status: str = "current"

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["candidates"] = [c.to_dict() for c in self.candidates]
        return d


def _overlap_groups(peaks: List[Peak], thr: float) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for kind in ("1H", "13C"):
        ks = sorted([p for p in peaks if p.kind == kind and p.shift is not None],
                    key=lambda p: (p.shift, p.id))
        cluster: List[Peak] = []
        for pk in ks:
            if cluster and abs(pk.shift - cluster[-1].shift) <= thr:
                cluster.append(pk)
            else:
                if len(cluster) > 1:
                    groups.append(_mk_overlap(kind, cluster, thr))
                cluster = [pk]
        if len(cluster) > 1:
            groups.append(_mk_overlap(kind, cluster, thr))
    return groups


def _mk_overlap(kind: str, cluster: List[Peak], thr: float) -> Dict[str, Any]:
    shifts = [p.shift for p in cluster]
    gid = "ov_" + digest({"kind": kind,
                          "shifts": sorted(_r6(s) for s in shifts),
                          "thr": thr})[:12]
    return {"id": gid, "kind": kind, "threshold_ppm": thr,
            "peak_ids": [p.id for p in cluster],
            "peak_refs": [p.ref for p in cluster],
            "shifts": [_r6(s) for s in shifts],
            "spread_ppm": _r6(max(shifts) - min(shifts)),
            "note": "重叠峰：不强制唯一匹配，簇内保留全部合法候选"}


def _symmetry_groups(atoms: Dict[str, Atom]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    by_kind: Dict[str, List[str]] = defaultdict(list)
    for label, a in atoms.items():
        if a.equivalence and a.equivalence > 1:
            by_kind[a.kind].append(label)
    for kind, labels in by_kind.items():
        gid = "sy_" + digest({"kind": kind, "labels": sorted(labels)})[:12]
        groups.append({"id": gid, "kind": kind, "nuclei": sorted(labels),
                       "note": "对称/等价原子：保留并列候选，不强行区分"})
    return groups


def _edge_maps(edges: List[Edge]) -> Dict[str, Any]:
    # peak_id -> set(neighbor peak_id)，按边种类
    out: Dict[str, Dict[str, set]] = {"COSY": defaultdict(set),
                                      "HSQC": defaultdict(set),
                                      "HMBC": defaultdict(set)}
    all_edges = []
    for e in edges:
        for a in e.a_peak_ids:
            for b in e.b_peak_ids:
                out[e.kind][a].add(b)
                out[e.kind][b].add(a)
                all_edges.append((e.kind, a, b, e.id))
    return {"neighbors": out, "all": all_edges}


def _skeleton_connectivity(atoms: Dict[str, Atom]) -> Dict[str, Any]:
    """由骨架键推断的相关期望（只作证据）。

    COSY：邻接（键距1）且两端为 H；HSQC：H 与同原子/直接相连 C；
    HMBC：键距 2~3 的 H-C 对。缺失这些边记为 missing-edge 证据。
    """
    labels = list(atoms.keys())
    adj: Dict[str, set] = {l: set() for l in labels}
    for l, a in atoms.items():
        for b in a.bonds:
            if b in adj:
                adj[l].add(b)
                adj[b].add(l)

    def dist(x: str, y: str, maxd: int = 3) -> int:
        if x == y:
            return 0
        seen = {x}
        frontier = [x]
        for d in range(1, maxd + 1):
            nxt = []
            for cur in frontier:
                for nb in adj[cur]:
                    if nb == y:
                        return d
                    if nb not in seen:
                        seen.add(nb)
                        nxt.append(nb)
            frontier = nxt
        return 99

    h_labels = [l for l in labels if atoms[l].kind == "1H"]
    c_labels = [l for l in labels if atoms[l].kind == "13C"]
    cosy = {(x, y) for i, x in enumerate(h_labels) for y in h_labels[i + 1:]
            if dist(x, y, 1) == 1}
    hsqc = set()
    hmbc = set()
    for h in h_labels:
        for c in c_labels:
            d = dist(h, c, 3)
            if d <= 1:
                hsqc.add((h, c))
            if d in (2, 3):
                hmbc.add((h, c))
    return {"cosy": cosy, "hsqc": hsqc, "hmbc": hmbc, "dist": dist,
            "adj": adj}


def _estimate_bias(dataset: Dataset, params: Dict[str, Any]) -> Dict[str, Any]:
    """按核种类估计整体化学位移偏置（仅 bias_correction 时使用）。"""
    atoms = dataset.atoms
    peaks = [p for p in dataset.peaks.values()]
    bias: Dict[str, Any] = {"applied": bool(params["bias_correction"]),
                            "by_kind": {}}
    for kind in ("1H", "13C"):
        # 用每种核预测值与最近峰的差作鲁棒候选，取中位数
        preds = sorted([a.predicted_shift for a in atoms.values()
                        if a.kind == kind and a.predicted_shift is not None])
        obs = sorted([p.shift for p in peaks
                      if p.kind == kind and p.shift is not None])
        residuals: List[float] = []
        if preds and obs:
            for pr in preds:
                nearest = min(obs, key=lambda o: abs(o - pr))
                residuals.append(round(nearest - pr, 6))
        if residuals:
            med = float(np.median(residuals))
            bias["by_kind"][kind] = {
                "offset_ppm": _r6(med),
                "min_bias_ppm": params["min_bias_ppm"][kind],
                "suspected": abs(med) >= params["min_bias_ppm"][kind],
                "residuals": sorted(residuals),
                "method": "median_nearest_residual"}
        else:
            bias["by_kind"][kind] = {"offset_ppm": 0.0,
                                     "min_bias_ppm": params["min_bias_ppm"][kind],
                                     "suspected": False,
                                     "residuals": [],
                                     "method": "median_nearest_residual"}
    return bias


def _overlap_groups_by_kind(peaks: List[Peak],
                            thresholds: Dict[str, float]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for kind in ("1H", "13C"):
        thr = thresholds.get(kind, 0.0)
        ks = sorted([p for p in peaks if p.kind == kind and p.shift is not None],
                    key=lambda p: (p.shift, p.id))
        cluster: List[Peak] = []
        for pk in ks:
            if cluster and abs(pk.shift - cluster[-1].shift) <= thr:
                cluster.append(pk)
            else:
                if len(cluster) > 1:
                    groups.append(_mk_overlap(kind, cluster, thr))
                cluster = [pk]
        if len(cluster) > 1:
            groups.append(_mk_overlap(kind, cluster, thr))
    return groups


def _multiplicity_match(atom: Atom, pk: Peak) -> Optional[bool]:
    if not atom.multiplicity or not pk.multiplicity:
        return None
    return atom.multiplicity == pk.multiplicity





def _candidate(atom: Atom, pk: Peak, status: str, score: Optional[float],
               rank: Optional[int], raw_res: Optional[float],
               corr_res: Optional[float], mult_match: Optional[bool],
               evidence: Dict[str, Any], reasons: List[str],
               symmetry_groups: List[Dict[str, Any]],
               peak_ov: Dict[str, str], pfp: str,
               direct_sources: List[str], tied: bool = False) -> Candidate:
    sym_group = next((g["id"] for g in symmetry_groups
                      if atom.label in g["nuclei"]), None)
    payload = {"nucleus": atom.label, "kind": atom.kind, "peak_id": pk.id,
               "predicted": atom.predicted_shift, "observed": pk.shift,
               "pfp": pfp}
    cid = "cd_" + digest(payload)[:14]
    return Candidate(
        id=cid, nucleus=atom.label, kind=atom.kind, peak_id=pk.id,
        peak_ref=pk.ref, status=status, score=score, rank=rank,
        raw_residual_ppm=raw_res, corrected_residual_ppm=corr_res,
        multiplicity_match=mult_match, multiplicity=pk.multiplicity,
        atom_multiplicity=atom.multiplicity, evidence=evidence,
        reject_reasons=list(reasons),
        symmetry={"equivalence": atom.equivalence,
                  "symmetry_group": sym_group,
                  "symmetric": bool(atom.equivalence and atom.equivalence > 1)},
        overlap_group=peak_ov.get(pk.id), direct_sources=sorted(direct_sources),
        algorithm_version=ENGINE_VERSION, params_fingerprint=pfp)


def _unknown_candidate(atom: Atom, pfp: str, direct_sources: List[str],
                       reason: str, partial: Optional[List[Candidate]] = None) -> Candidate:
    cid = "cd_" + digest({"nucleus": atom.label, "kind": atom.kind,
                          "unknown": True, "pfp": pfp})[:14]
    return Candidate(
        id=cid, nucleus=atom.label, kind=atom.kind, peak_id=None,
        peak_ref=None, status="unknown", score=None, rank=None,
        raw_residual_ppm=None, corrected_residual_ppm=None,
        multiplicity_match=None, multiplicity=None,
        atom_multiplicity=atom.multiplicity,
        evidence={"note": "无足够证据；未知状态为一等结果，不以最新计算覆盖"},
        reject_reasons=[reason],
        symmetry={"equivalence": atom.equivalence, "symmetry_group": None,
                  "symmetric": bool(atom.equivalence and atom.equivalence > 1)},
        overlap_group=None, direct_sources=sorted(direct_sources),
        algorithm_version=ENGINE_VERSION, params_fingerprint=pfp)


def _ambiguity_groups(candidates: List[Candidate], atoms: Dict[str, Atom],
                      pid_to_peak: Dict[str, Peak],
                      overlap_groups: List[Dict[str, Any]],
                      symmetry_groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把“无法唯一决定”的情形固化为一等歧义组。

    两类：
    - OVERLAP_PEAKS：重叠峰簇相关核的并列候选；
    - SYMMETRIC_ATOMS：对称核的并列候选。
    组内全部合法候选保留，人工只能对整组或显式选择作出裁定。
    """
    groups: List[Dict[str, Any]] = []
    live = [c for c in candidates if c.status == "candidate" and c.rank == 1]

    # 重叠峰驱动
    for og in overlap_groups:
        peak_ids = set(og["peak_ids"])
        involved: Dict[str, List[str]] = defaultdict(list)
        for c in live:
            if c.peak_id in peak_ids:
                involved[c.nucleus].append(c.id)
        if involved:
            gid = "ag_" + digest({"type": "OVERLAP_PEAKS",
                                  "overlap": og["id"],
                                  "nuclei": dict(sorted(involved.items()))})[:14]
            groups.append({
                "id": gid, "type": "OVERLAP_PEAKS",
                "overlap_group": og["id"],
                "nuclei": sorted(involved.keys()),
                "candidate_ids": sorted(cid for ids in involved.values() for cid in ids),
                "resolution": "ambiguous",
                "rule": "重叠峰不能被强制唯一匹配；保留全部合法候选",
                "decidable_as_unit": True})

    # 对称核驱动
    for sg in symmetry_groups:
        nuclei = set(sg["nuclei"])
        involved = {c.nucleus: c.id for c in live if c.nucleus in nuclei and c.rank == 1}
        if len(involved) >= 2:
            cands_for = defaultdict(list)
            for c in live:
                if c.nucleus in nuclei:
                    cands_for[c.nucleus].append(c.id)
            gid = "ag_" + digest({"type": "SYMMETRIC_ATOMS",
                                  "symmetry": sg["id"],
                                  "nuclei": sorted(cands_for)})[:14]
            groups.append({
                "id": gid, "type": "SYMMETRIC_ATOMS",
                "symmetry_group": sg["id"],
                "nuclei": sorted(cands_for.keys()),
                "candidate_ids": sorted(cid for ids in cands_for.values() for cid in ids),
                "resolution": "ambiguous",
                "rule": "对称原子不强行区分；全部并列候选保留，旧决定可重放",
                "decidable_as_unit": True})

    # 单纯的并列最优（即便没有重叠/对称）也要保留
    by_nuc: Dict[str, List[str]] = defaultdict(list)
    for c in live:
        by_nuc[c.nucleus].append(c.id)
    existing_cids = {cid for g in groups for cid in g["candidate_ids"]}
    for nuc, ids in by_nuc.items():
        if len(ids) > 1 and not all(i in existing_cids for i in ids):
            gid = "ag_" + digest({"type": "TIED_BEST", "nucleus": nuc,
                                  "candidates": sorted(ids)})[:14]
            groups.append({"id": gid, "type": "TIED_BEST", "nuclei": [nuc],
                           "candidate_ids": sorted(ids),
                           "resolution": "ambiguous",
                           "rule": "并列最优候选全部保留",
                           "decidable_as_unit": True})
    return groups


def _bias_findings(bias: Dict[str, Any], params: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for kind, info in bias["by_kind"].items():
        if info["suspected"]:
            out.append({
                "code": "GLOBAL_SHIFT_BIAS", "level": "warning", "kind": kind,
                "offset_ppm": info["offset_ppm"],
                "correction_applied": bool(params["bias_correction"]),
                "min_bias_ppm": info["min_bias_ppm"],
                "basis": ("检测到化学位移整体偏置；"
                          + ("已按整体平移校正（不改变峰间相对结构）"
                             if params["bias_correction"]
                             else "未启用校正时超差候选保持被拒绝/未知，不强行归属"))})
    return out


def _overlap_findings(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"code": "OVERLAPPING_PEAKS", "level": "info",
             "overlap_group": g["id"], "kind": g["kind"],
             "spread_ppm": g["spread_ppm"],
             "peak_refs": g["peak_refs"],
             "basis": g["note"]} for g in groups]


def _symmetry_findings(groups: List[Dict[str, Any]],
                       atoms: Dict[str, Atom]) -> List[Dict[str, Any]]:
    out = []
    for g in groups:
        out.append({"code": "SYMMETRIC_ATOMS", "level": "info",
                    "symmetry_group": g["id"], "kind": g["kind"],
                    "nuclei": g["nuclei"],
                    "basis": g["note"]})
    return out


def _shift_score(residual: float, tol: float) -> float:
    if tol <= 0:
        return 0.0 if abs(residual) == 0 else 1.0
    return min(1.0, abs(residual) / tol)


def run_engine(dataset: Dataset, params: Dict[str, Any], created_at: str,
               context_fingerprint: str, inputs_hash: str,
               direct_source_ids: List[str]) -> Snapshot:
    params = normalize_params(params)
    pfp = params_fingerprint(params)
    atoms = dataset.atoms
    peaks = list(dataset.peaks.values())
    overlap_groups = _overlap_groups_by_kind(peaks, params["overlap_threshold_ppm"])
    symmetry_groups = _symmetry_groups(atoms)
    emaps = _edge_maps(dataset.edges)
    conn = _skeleton_connectivity(atoms)
    bias = _estimate_bias(dataset, params)

    peak_ov: Dict[str, str] = {}
    for g in overlap_groups:
        for pid in g["peak_ids"]:
            peak_ov[pid] = g["id"]

    candidates: List[Candidate] = []
    viable: Dict[str, List[Dict[str, Any]]] = {}
    for label, atom in sorted(atoms.items()):
        kind = atom.kind
        viable[label] = []
        if atom.predicted_shift is None:
            candidates.append(_unknown_candidate(
                atom, pfp, direct_source_ids,
                "NO_PREDICTION: 无预测化学位移，保留未知状态"))
            continue
        offset = bias["by_kind"][kind]["offset_ppm"] if params["bias_correction"] else 0.0
        tol = params["tolerance_ppm"][kind]
        for pk in sorted([p for p in peaks if p.kind == kind and p.shift is not None],
                         key=lambda p: (p.shift, p.id)):
            raw_res = round(pk.shift - atom.predicted_shift, 6)
            corr_res = round(pk.shift - (atom.predicted_shift + offset), 6)
            used = corr_res if params["bias_correction"] else raw_res
            mult_match = _multiplicity_match(atom, pk)
            reasons: List[str] = []
            base = _shift_score(used, tol)
            if abs(used) > tol:
                reasons.append(
                    f"SHIFT_OUT_OF_TOLERANCE: |residual|={abs(used):.3f}ppm > tol={tol}ppm")
            if mult_match is False:
                base += params["weights"]["multiplicity_mismatch"]
            cand = _candidate(
                atom, pk, "rejected" if reasons else "candidate",
                None, None, raw_res, corr_res, mult_match,
                {"present_edges": {"COSY": [], "HSQC": [], "HMBC": []},
                 "missing_edges": {"COSY": [], "HSQC": [], "HMBC": []},
                 "unexpected_edges": {"COSY": [], "HMBC": []},
                 "note": "耦合网络只作证据"},
                reasons, symmetry_groups, peak_ov, pfp, direct_source_ids)
            cand.score = round(base, 6)
            candidates.append(cand)
            if not reasons:
                viable[label].append({"candidate": cand, "peak": pk})
        if not viable[label]:
            candidates.append(_unknown_candidate(
                atom, pfp, direct_source_ids,
                "NO_CANDIDATE_IN_TOLERANCE: 容差内无峰（检查整体偏置/参考标准）"))

    _apply_coupling_evidence(atoms, viable, conn, emaps, None, candidates, params)

    by_nuc: Dict[str, List[Candidate]] = defaultdict(list)
    for c in candidates:
        by_nuc[c.nucleus].append(c)
    findings: List[Dict[str, Any]] = []
    for label, cands in by_nuc.items():
        live = sorted([c for c in cands if c.status == "candidate"],
                      key=lambda c: (c.score if c.score is not None else 1e9,
                                     c.peak_id or ""))
        for c in cands:
            c.rank = None
        if live:
            best = live[0].score
            for c in live:
                if c.score == best:
                    c.rank = 1
            ties = [c for c in live if c.score == best]
            if len(ties) > 1:
                findings.append({
                    "code": "TIED_BEST_CANDIDATES", "level": "info",
                    "nucleus": label, "kind": ties[0].kind,
                    "best_score": best, "tied_peak_ids": [c.peak_id for c in ties],
                    "basis": "并列最优候选全部保留 rank=1；耦合网络仅作证据，不唯一化"})

    pid_to_peak = {p.id: p for p in peaks}
    ambiguity_groups = _ambiguity_groups(candidates, atoms, pid_to_peak,
                                         overlap_groups, symmetry_groups)
    findings.extend(_missing_edge_findings(candidates))
    findings.extend(_bias_findings(bias, params))
    findings.extend(_overlap_findings(overlap_groups))
    findings.extend(_symmetry_findings(symmetry_groups, atoms))

    direct = sorted(direct_source_ids)
    snapshot_id = "sn_" + digest({
        "inputs_hash": inputs_hash, "engine": ENGINE_VERSION, "params": pfp,
        "context": context_fingerprint,
        "candidates": [c.id for c in sorted(candidates, key=lambda x: x.id)],
    })[:14]
    return Snapshot(
        id=snapshot_id, inputs_hash=inputs_hash,
        algorithm_version=ENGINE_VERSION, params_fingerprint=pfp,
        created_at=created_at, bias=bias, overlap_groups=overlap_groups,
        symmetry_groups=symmetry_groups, candidates=candidates,
        ambiguity_groups=ambiguity_groups, findings=findings,
        direct_sources=direct, context_fingerprint=context_fingerprint)


def pid_meet(candidate_ids: List[str], observed: set) -> bool:
    return any(pid in observed for pid in candidate_ids)


def _apply_coupling_evidence(atoms, viable, conn, emaps, pid_to_peak,
                             candidates, params) -> None:
    """对每个存活候选评估 COSY/HSQC/HMBC 证据；缺失/意外边只罚分与标注。"""
    neighbours = emaps["neighbors"]
    weights = params["weights"]
    for label, options in viable.items():
        atom = atoms[label]
        for opt in options:
            cand = opt["candidate"]
            pk = opt["peak"]
            present = {"COSY": [], "HSQC": sorted(neighbours["HSQC"].get(pk.id, set())),
                       "HMBC": []}
            missing = {"COSY": [], "HSQC": [], "HMBC": []}
            unexpected = {"COSY": [], "HMBC": []}
            penalty = 0.0
            other_peaks = {other: [o["peak"].id for o in viable.get(other, [])]
                           for other in atoms if other != label}
            for other, opeak_ids in other_peaks.items():
                d = conn["dist"](label, other, 3)
                if atom.kind == "1H" and atoms[other].kind == "1H" and d == 1:
                    hit = [pid for pid in opeak_ids
                           if pid in neighbours["COSY"].get(pk.id, set())]
                    if hit:
                        present["COSY"].append({"nucleus": other, "peak_ids": hit})
                    else:
                        missing["COSY"].append({
                            "nucleus": other,
                            "reason": "骨架邻接 H-H 但观测无 COSY 边（缺失相关边只作证据）"})
                        penalty += weights["missing_cosy"]
                if {atom.kind, atoms[other].kind} == {"1H", "13C"} and d <= 1:
                    hit = [pid for pid in opeak_ids
                           if pid in neighbours["HSQC"].get(pk.id, set())]
                    if not hit:
                        missing["HSQC"].append({
                            "nucleus": other,
                            "reason": "直接 H-C 但观测无 HSQC 边（只作证据）"})
                if {atom.kind, atoms[other].kind} == {"1H", "13C"} and d in (2, 3):
                    hit = [pid for pid in opeak_ids
                           if pid in neighbours["HMBC"].get(pk.id, set())]
                    if hit:
                        present["HMBC"].append({"nucleus": other, "peak_ids": hit})
                    else:
                        missing["HMBC"].append({
                            "nucleus": other,
                            "reason": "键距2~3 H-C 但观测无 HMBC 边（缺失相关边只作证据）"})
                        penalty += weights["missing_hmbc"]
                if d == 99:
                    if pid_meet(opeak_ids, neighbours["COSY"].get(pk.id, set())):
                        unexpected["COSY"].append({"nucleus": other})
                        penalty += weights["unexpected_cosy"]
                    if pid_meet(opeak_ids, neighbours["HMBC"].get(pk.id, set())):
                        unexpected["HMBC"].append({"nucleus": other})
                        penalty += weights["unexpected_hmbc"]
            cand.evidence["present_edges"] = present
            cand.evidence["missing_edges"] = missing
            cand.evidence["unexpected_edges"] = unexpected
            cand.score = round((cand.score or 0.0) + penalty, 6)


def _missing_edge_findings(candidates: List[Candidate]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for c in candidates:
        if c.status != "candidate":
            continue
        for ek in ("COSY", "HSQC", "HMBC"):
            for item in c.evidence.get("missing_edges", {}).get(ek, []):
                partner = item.get("nucleus") if isinstance(item, dict) else None
                key = ("MISSING_EDGE", ek, c.nucleus, partner)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"code": "MISSING_CORRELATION_EDGE", "level": "info",
                            "edge_kind": ek, "nucleus": c.nucleus,
                            "expected_partner": partner, "candidate_id": c.id,
                            "basis": (item.get("reason") if isinstance(item, dict)
                                      else "缺失相关边只作证据，不补造")})
    return out
