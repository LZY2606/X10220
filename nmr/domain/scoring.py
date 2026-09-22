"""候选生成与打分（领域规则，不产出唯一数字）。

设计原则：
- 化学位移误差用 numpy 距离矩阵全量计算，容差外的配对不是“删除”，
  而是保留为 rejected 候选并记录拒绝原因（shift_out_of_tolerance）；
- 重叠峰：观测位移彼此落在 overlap 阈值内 -> overlap_group，
  系统绝不强制唯一匹配，组内全部合法候选保留；
- 对称原子：等价原子的候选同分时记 tied（symmetry_tie），不自动拆分；
- 缺失相关边：耦合网络只作证据；期望存在但原始文件中缺失的边产生
  missing_edge 诊断并仅施加封顶小罚分，不能因此拒绝候选；
- 并列最优：多个候选同为最低误差 -> tied=true，全部保留；
- 全局偏置：观测值先减 bias 再与预测值比较；偏置是参数版本的一部分，
  参数修订后只有依赖候选过期。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from .state import (graph_distance, overlap_threshold, tolerance_for,
                    bias_for)


def corrected_shift(observed_ppm: float, nucleus: str,
                    params: Dict[str, Any]) -> float:
    return observed_ppm - bias_for(nucleus, params)


def detect_overlap_groups(peaks: List[Dict[str, Any]],
                          params: Dict[str, Any]) -> Dict[str, str]:
    """返回 peak_key -> overlap_group id（组内至少两个可转换观测峰）。"""
    by_nucleus: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
    for pk in peaks:
        shift = pk["shift_value"]
        if shift is None:
            continue
        by_nucleus[pk["nucleus"]].append((float(shift), pk["key"]))
    mapping: Dict[str, str] = {}
    for nucleus, items in by_nucleus.items():
        items.sort()
        thr = overlap_threshold(nucleus, params)
        i = 0
        group_n = 0
        while i < len(items):
            j = i + 1
            while j < len(items) and abs(items[j][0] - items[i][0]) <= thr:
                j += 1
            if j - i > 1:
                group_n += 1
                gid = f"ov_{nucleus.replace('1', '')}_{group_n}"
                for _, key in items[i:j]:
                    mapping[key] = gid
            i = j
    return mapping


def expected_coupling_pairs(graph: Dict[str, Any], h_atoms: List[Dict[str, Any]],
                            c_atoms: List[Dict[str, Any]],
                            curated: Optional[Dict[str, List[List[str]]]] = None
                            ) -> Dict[str, Any]:
    """根据骨架计算相关边的期望集合（耦合网络的“证据期望”）。

    COSY: 1H-1H 经 3 键（H 挂在碳上，碳-碳相邻 -> 距离 1）。
    HSQC: H 与直接挂载的 C（attached_to）。
    HMBC: H 到碳图上距离 2~3 的 C（2/3 键远程相关）。
    """
    h_by_label = {a["label"]: a for a in h_atoms}
    c_labels = {a["label"] for a in c_atoms}
    if curated is not None:
        return {exp: {tuple(sorted(pair)) for pair in curated.get(exp, [])}
                for exp in ("COSY", "HSQC", "HMBC")}
    expected = {"COSY": set(), "HSQC": set(), "HMBC": set()}
    for hi, ai in enumerate(h_atoms):
        host_i = ai.get("attached_to")
        for hj in h_atoms:
            host_j = hj.get("attached_to")
            if host_i and host_j and host_i != host_j:
                dist = graph_distance(graph, host_i, host_j, max_dist=3)
                if dist == 1:
                    pair = tuple(sorted((ai["label"], hj["label"])))
                    expected["COSY"].add(pair)
        if host_i:
            expected["HSQC"].add((ai["label"], host_i))
            for clabel in c_labels:
                dist = graph_distance(graph, host_i, clabel, max_dist=4)
                if dist in (2, 3):
                    expected["HMBC"].add((ai["label"], clabel))
    return expected


def resolve_edge_refs(edges: List[Dict[str, Any]],
                      peaks_by_key: Dict[str, Dict[str, Any]],
                      h_atoms: List[Dict[str, Any]],
                      c_atoms: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把二维边的 from/to 引用解析到 (peak_key, atom_label/peak_key)。

    引用可以是一维 peak key，也可以是骨架原子 label；原始引用字符串始终保留。
    """
    atom_labels = {a["label"] for a in h_atoms} | {a["label"] for a in c_atoms}
    resolved: Dict[str, List[Dict[str, Any]]] = {"COSY": [], "HSQC": [], "HMBC": []}
    for edge in edges:
        resolved[edge["experiment"]].append({
            "stable_id": edge["stable_id"],
            "from_ref": edge["from_ref"],
            "to_ref": edge["to_ref"],
            "from_is_atom": edge["from_ref"] in atom_labels,
            "to_is_atom": edge["to_ref"] in atom_labels,
            "from_peak": edge["from_ref"] if edge["from_ref"] in peaks_by_key else None,
            "to_peak": edge["to_ref"] if edge["to_ref"] in peaks_by_key else None,
        })
    return resolved


def _edge_lookup(resolved: Dict[str, List[Dict[str, Any]]], experiment: str
                 ) -> Tuple[Set[Tuple[str, str]], Set[str]]:
    pairs: Set[Tuple[str, str]] = set()
    edge_ids: Set[str] = set()
    for edge in resolved.get(experiment, []):
        pairs.add((edge["from_ref"], edge["to_ref"]))
        pairs.add((edge["to_ref"], edge["from_ref"]))
        edge_ids.add(edge["stable_id"])
    return pairs, edge_ids


def _edge_matches(pairs: Set[Tuple[str, str]], a: str, b: str,
                  peaks_for_atom: Dict[str, List[str]]) -> Optional[str]:
    """证据匹配：原子对之间，或它们任一观测峰之间存在边。"""
    if (a, b) in pairs:
        return "direct_atom"
    for pa in peaks_for_atom.get(a, ()):
        for pb in peaks_for_atom.get(b, ()):
            if (pa, pb) in pairs or (pa, b) in pairs or (a, pb) in pairs:
                return "via_peak"
    return None


def compute_candidates(*, peaks: List[Dict[str, Any]],
                       h_atoms: List[Dict[str, Any]],
                       c_atoms: List[Dict[str, Any]],
                       bonds: List[Dict[str, Any]],
                       edges: List[Dict[str, Any]],
                       params: Dict[str, Any],
                       rng: np.random.Generator,
                       curated_edges: Optional[Dict[str, List[List[str]]]] = None
                       ) -> Dict[str, Any]:
    """对全部 1H/13C 观测峰计算候选网络。返回结构可直接入快照事件。"""
    graph = {"labels": set(), "adj": defaultdict(set)}
    for a in h_atoms + c_atoms:
        graph["labels"].add(a["label"])
    for b in bonds:
        graph["adj"][b["from"]].add(b["to"])
        graph["adj"][b["to"]].add(b["from"])

    peaks_by_key = {p["key"]: p for p in peaks}
    overlap_map = detect_overlap_groups(peaks, params)
    resolved = resolve_edge_refs(edges, peaks_by_key, h_atoms, c_atoms)
    cosy_pairs, cosy_edge_ids = _edge_lookup(resolved, "COSY")
    hsqc_pairs, hsqc_edge_ids = _edge_lookup(resolved, "HSQC")
    hmbc_pairs, hmbc_edge_ids = _edge_lookup(resolved, "HMBC")
    expected = expected_coupling_pairs(graph, h_atoms, c_atoms, curated_edges)

    # 对称等价类
    sg: Dict[str, List[str]] = defaultdict(list)
    for a in h_atoms + c_atoms:
        sg[a["symmetry_group"]].append(a["label"])
    symmetry_classes: Dict[str, Set[str]] = {}
    for labels in sg.values():
        if len(labels) > 1:
            for label in labels:
                symmetry_classes[label] = set(labels)

    # 第一轮：位移候选（仅可转换观测峰参与），建立 atom -> plausible peaks 反查表
    plausible_peaks: Dict[str, List[str]] = defaultdict(list)
    shift_errors: Dict[Tuple[str, str], float] = {}
    candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    atoms_by_nucleus = {"1H": h_atoms, "13C": c_atoms}
    for nucleus in ("1H", "13C"):
        obs = [p for p in peaks if p["nucleus"] == nucleus and p["shift_value"] is not None]
        atoms = atoms_by_nucleus[nucleus]
        if not obs or not atoms:
            continue
        obs_corr = np.array([corrected_shift(p["shift_value"], nucleus, params)
                             for p in obs], dtype=float)
        pred = np.array([a["predicted_value"] for a in atoms], dtype=float)
        diff = np.abs(obs_corr[:, None] - pred[None, :])
        tol = tolerance_for(nucleus, params)
        weights = params["score_weights"]

        for i, peak in enumerate(obs):
            errors = diff[i]
            order = np.argsort(errors, kind="stable")
            for j in order:
                atom = atoms[int(j)]
                err = float(errors[int(j)])
                shift_errors[(peak["key"], atom["label"])] = round(err, 6)
                base = {
                    "peak_key": peak["key"],
                    "nucleus": nucleus,
                    "atom_label": atom["label"],
                    "symmetry_group": atom["symmetry_group"],
                    "shift_error_ppm": round(err, 6),
                    "corrected_shift_ppm": round(float(obs_corr[i]), 6),
                    "predicted_shift_ppm": round(float(pred[int(j)]), 6),
                    "overlap_group": overlap_map.get(peak["key"]),
                    "plausible": bool(err <= tol),
                }
                if err > tol:
                    rejected.append({**base,
                                     "reject_reason": "shift_out_of_tolerance",
                                     "tolerance_ppm": tol})
                else:
                    plausible_peaks[atom["label"]].append(peak["key"])
                    candidates.append(base)

    # 第二轮：为合法候选附加耦合证据、缺边诊断与总分
    missing_diagnostics: List[Dict[str, Any]] = []
    for cand in candidates:
        nucleus = cand["nucleus"]
        atom = cand["atom_label"]
        peak = cand["peak_key"]
        weights = params["score_weights"]
        evidence: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []
        penalty = 0.0

        atom_record = next(a for a in h_atoms + c_atoms if a["label"] == atom)

        if nucleus == "1H":
            host = atom_record.get("attached_to")
            if host:
                match = _edge_matches(hsqc_pairs, atom, host, plausible_peaks)
                if match:
                    evidence.append({"type": "HSQC", "atom": host, "match": match,
                                     "weight": weights["hsqc"]})
                else:
                    penalty += weights["missing_edge_penalty"]
                    info = {"type": "HSQC", "from": atom, "to": host,
                             "reason": "expected_direct_correlation_absent",
                             "capped_penalty": weights["missing_edge_penalty"]}
                    missing.append(info)
                    missing_diagnostics.append({**info, "peak_key": peak})
            for pair in sorted(expected["COSY"]):
                if atom not in pair:
                    continue
                other = pair[0] if pair[1] == atom else pair[1]
                match = _edge_matches(cosy_pairs, atom, other, plausible_peaks)
                if match:
                    evidence.append({"type": "COSY", "atom": other, "match": match,
                                     "weight": weights["cosy"]})
                else:
                    penalty += weights["missing_edge_penalty"]
                    info = {"type": "COSY", "from": atom, "to": other,
                             "reason": "expected_3j_correlation_absent",
                             "capped_penalty": weights["missing_edge_penalty"]}
                    missing.append(info)
                    missing_diagnostics.append({**info, "peak_key": peak})
            for pair in sorted(expected["HMBC"]):
                if atom not in pair:
                    continue
                other = pair[0] if pair[1] == atom else pair[1]
                match = _edge_matches(hmbc_pairs, atom, other, plausible_peaks)
                if match:
                    evidence.append({"type": "HMBC", "atom": other, "match": match,
                                     "weight": weights["hmbc"]})
        else:  # 13C：查找挂在该碳上的 H 的 HSQC，及从其他 H 来的 HMBC
            attached = [a["label"] for a in h_atoms if a.get("attached_to") == atom]
            for hlabel in attached:
                if _edge_matches(hsqc_pairs, hlabel, atom, plausible_peaks):
                    evidence.append({"type": "HSQC", "atom": hlabel, "match": "direct_atom",
                                     "weight": weights["hsqc"]})
            for h_atom in h_atoms:
                pair = (h_atom["label"], atom)
                if pair in expected["HMBC"]:
                    if _edge_matches(hmbc_pairs, h_atom["label"], atom, plausible_peaks):
                        evidence.append({"type": "HMBC", "atom": h_atom["label"],
                                         "match": "direct_atom",
                                         "weight": weights["hmbc"]})

        support = sum(e["weight"] for e in evidence)
        # 分数 = 归一化位移误差 + 罚分 - 证据支持（证据仅加减，不能独立决定）
        tol = tolerance_for(nucleus, params)
        normalized_error = cand["shift_error_ppm"] / tol if tol else 0.0
        score = round(weights["shift_error"] * normalized_error
                      + penalty - support + float(rng.normal(0.0, 1e-9)), 9)
        cand["score"] = score
        cand["evidence"] = evidence
        cand["missing_edges"] = missing
        cand["missing_edge_penalty"] = round(penalty, 6)
        cand["symmetry_equivalent_to"] = sorted(
            symmetry_classes.get(atom, set()) - {atom})

    # 每个峰：判定并列最优（同分 + 数值等价阈值）与对称性平局
    tie_window = 1e-6
    peak_to_cands: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for cand in candidates:
        peak_to_cands[cand["peak_key"]].append(cand)
    for peak_key, cands in peak_to_cands.items():
        cands.sort(key=lambda c: (c["score"], c["atom_label"]))
        best = cands[0]["score"]
        top = [c for c in cands if abs(c["score"] - best) <= tie_window]
        best_labels = {c["atom_label"] for c in top}
        overlap_group = cands[0]["overlap_group"]
        symmetry_tie = any(c["symmetry_equivalent_to"] for c in top)
        for rank, cand in enumerate(cands, start=1):
            cand["rank"] = rank
            cand["best"] = cand in top
            cand["tied"] = len(top) > 1
            cand["tie_type"] = (
                "symmetry_tie" if symmetry_tie else "score_tie"
            ) if len(top) > 1 else None

    # 未观测/无合法候选的原子 -> 一等 unknown
    unknowns: List[Dict[str, Any]] = []
    plausible_atoms = {c["atom_label"] for c in candidates}
    for a in h_atoms + c_atoms:
        if a["predicted_value"] is None:
            unknowns.append({"atom_label": a["label"], "nucleus": a["nucleus"],
                             "reason": "predicted_shift_unconvertible",
                             "symmetry_group": a["symmetry_group"]})
        elif a["label"] not in plausible_atoms:
            unknowns.append({"atom_label": a["label"], "nucleus": a["nucleus"],
                             "reason": "no_peak_in_tolerance",
                             "symmetry_group": a["symmetry_group"]})

    unconvertible_peaks = [{"key": p["key"], "nucleus": p["nucleus"],
                            "reason": "observed_shift_unconvertible",
                            "raw": p["shift_raw"]}
                           for p in peaks if p["shift_value"] is None]

    # 汇总：哪些观测峰落入重叠组（不能被强制唯一匹配）
    overlap_groups: Dict[str, List[str]] = defaultdict(list)
    for pk, gid in overlap_map.items():
        overlap_groups[gid].append(pk)

    return {
        "candidates": candidates,
        "rejected": rejected,
        "unknowns": unknowns,
        "unconvertible_peaks": unconvertible_peaks,
        "missing_edge_diagnostics": missing_diagnostics,
        "overlap_groups": {k: sorted(v) for k, v in sorted(overlap_groups.items())},
        "expected_edges": {k: sorted(list(v)) for k, v in expected.items()},
        "edge_evidence_sources": {
            "COSY": sorted(cosy_edge_ids), "HSQC": sorted(hsqc_edge_ids),
            "HMBC": sorted(hmbc_edge_ids)},
    }
