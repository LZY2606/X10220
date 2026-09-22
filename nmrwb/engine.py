"""领域计算：归属候选生成。

原则：
- 重叠峰和缺峰不能被强制唯一匹配：合法候选全部保留；
- 耦合网络（COSY/HSQC/HMBC）只作为证据，影响分数，不做硬排除；
- 缺失预期相关边只产生诊断并降低证据权重；
- 化学位移整体偏置作为一等诊断，而不是悄悄修正结果；
- 并列最优候选全部保留；对称原子标注为可互换；
- 候选、拒绝原因（未知）、误差、诊断都是一等数据；
- 每个候选记录直接来源（depends_on），局部输入变化只使其过期。
"""
from __future__ import annotations

from typing import Dict, List, Set

import numpy as np

from .core import fingerprint, stable_id

NUCLEUS_OF_ELEMENT = {"H": "1H", "C": "13C"}


def compute_candidates(inputs: Dict, params: Dict, deps: Dict[str, str],
                       params_id: str) -> Dict:
    """inputs: name -> {"format", "parsed"}；deps: name -> input_id。"""
    if "mol" not in inputs:
        raise ValueError("缺少分子骨架输入 (name=mol)")
    mol = inputs["mol"]["parsed"]
    atoms = mol["atoms"]
    bonds = mol["bonds"]

    tol = float(params["shift_tolerance_ppm"])
    otol = float(params["overlap_tolerance_ppm"])
    weight = float(params["edge_evidence_weight"])
    tie_eps = float(params["tie_epsilon"])
    bias_thr = float(params["bias_threshold_ppm"])

    peaks_by_nuc: Dict[str, list] = {}
    nuc_dep: Dict[str, str] = {}
    conditions: Dict[str, dict] = {}
    edge_sources = []
    for name, entry in inputs.items():
        if entry["format"] == "nmrwb-1d":
            nuc = entry["parsed"]["meta"].get("nucleus")
            if nuc:
                peaks_by_nuc[nuc] = entry["parsed"]["peaks"]
                nuc_dep[nuc] = deps[name]
                conditions[nuc] = dict(entry["parsed"]["meta"])
        elif entry["format"] == "nmrwb-2d":
            etype = entry["parsed"]["meta"].get("type", "").upper()
            edge_sources.append((name, etype, entry["parsed"]["edges"]))

    diagnostics: List[Dict] = []

    # 重叠峰分组（同一核内，相邻差 <= overlap_tolerance）。
    overlap_group_of = {}
    for nuc, peaks in peaks_by_nuc.items():
        valid = sorted((p for p in peaks if p["shift"] is not None),
                       key=lambda p: p["shift"])
        gid = 0
        i = 0
        while i < len(valid):
            j = i
            while (j + 1 < len(valid)
                   and valid[j + 1]["shift"] - valid[i]["shift"] <= otol):
                j += 1
            if j > i:
                gid += 1
                for k in range(i, j + 1):
                    overlap_group_of[valid[k]["peak_id"]] = "%s-og%d" % (nuc, gid)
            i = j + 1

    bonded: Dict[str, Set[str]] = {}
    for b in bonds:
        bonded.setdefault(b["a"], set()).add(b["b"])
        bonded.setdefault(b["b"], set()).add(b["a"])

    def bfs_dist(src, dst):
        if src == dst:
            return 0
        seen = {src}
        frontier = [src]
        d = 0
        while frontier:
            d += 1
            nxt = []
            for n in frontier:
                for m in bonded.get(n, ()):
                    if m == dst:
                        return d
                    if m not in seen:
                        seen.add(m)
                        nxt.append(m)
            frontier = nxt
        return None

    sym_groups: Dict[str, List[str]] = {}
    for a in atoms:
        if a.get("sym"):
            sym_groups.setdefault(a["sym"], []).append(a["atom_id"])

    h_atoms = [a for a in atoms if a["element"] == "H"]
    c_atoms = [a for a in atoms if a["element"] == "C"]
    c_ids = {a["atom_id"] for a in c_atoms}
    attached = {}
    for h in h_atoms:
        attached[h["atom_id"]] = next(
            (n for n in bonded.get(h["atom_id"], ()) if n in c_ids), None)

    candidates = []
    unknowns = []
    for a in atoms:
        nuc = NUCLEUS_OF_ELEMENT.get(a["element"])
        if nuc is None or nuc not in peaks_by_nuc:
            unknowns.append({"atom_id": a["atom_id"], "nucleus": nuc,
                             "reason": "无匹配的一维峰列表；不强制唯一匹配"})
            continue
        if a["range"] is None:
            unknowns.append({"atom_id": a["atom_id"], "nucleus": nuc,
                             "reason": "预测位移范围无法解析，原始值保留于输入"})
            continue
        lo, hi = a["range"]
        center = (lo + hi) / 2.0
        half = (hi - lo) / 2.0 + tol
        for p in peaks_by_nuc[nuc]:
            if p["shift"] is None:
                continue
            err = p["shift"] - center
            if abs(err) <= half:
                base = 1.0 / (1.0 + abs(err) / max(tol, 1e-9))
                candidates.append({
                    "candidate_id": stable_id("cand", a["atom_id"], p["peak_id"]),
                    "atom_id": a["atom_id"], "peak_id": p["peak_id"],
                    "nucleus": nuc, "shift_ppm": p["shift"],
                    "predicted_center_ppm": round(center, 6),
                    "err_ppm": round(err, 6),
                    "base_score": round(base, 6), "score": round(base, 6),
                    "evidence": [], "flags": [], "status": "alternate",
                    "_srcs": set(),
                })
    cand_index = {(c["atom_id"], c["peak_id"]): c for c in candidates}

    def find_peak(nuc, shift):
        best, bd = None, None
        for p in peaks_by_nuc.get(nuc, []):
            if p["shift"] is None:
                continue
            d = abs(p["shift"] - shift)
            if d <= tol and (bd is None or d < bd):
                best, bd = p, d
        return best

    def add_ev(atom_id, peak, edge_id, etype, src):
        if peak is None:
            return
        c = cand_index.get((atom_id, peak["peak_id"]))
        if c is not None and all(ev["edge_id"] != edge_id for ev in c["evidence"]):
            c["evidence"].append({"edge_id": edge_id, "type": etype})
            c["_srcs"].add(src)

    for src, etype, edges in edge_sources:
        for e in edges:
            if e["f1"] is None or e["f2"] is None:
                continue
            if etype == "HSQC":
                ph, pc = find_peak("1H", e["f1"]), find_peak("13C", e["f2"])
                if ph and pc:
                    for h in h_atoms:
                        ca = attached.get(h["atom_id"])
                        if ca:
                            add_ev(h["atom_id"], ph, e["edge_id"], etype, src)
                            add_ev(ca, pc, e["edge_id"], etype, src)
            elif etype == "COSY":
                p1, p2 = find_peak("1H", e["f1"]), find_peak("1H", e["f2"])
                if p1 and p2:
                    for ha in h_atoms:
                        ca = attached.get(ha["atom_id"])
                        if not ca:
                            continue
                        for hb in h_atoms:
                            if hb["atom_id"] <= ha["atom_id"]:
                                continue
                            cb = attached.get(hb["atom_id"])
                            if not cb or cb == ca:
                                continue
                            if cb in bonded.get(ca, ()):
                                add_ev(ha["atom_id"], p1, e["edge_id"], etype, src)
                                add_ev(hb["atom_id"], p2, e["edge_id"], etype, src)
                                add_ev(ha["atom_id"], p2, e["edge_id"], etype, src)
                                add_ev(hb["atom_id"], p1, e["edge_id"], etype, src)
            elif etype == "HMBC":
                ph, pc = find_peak("1H", e["f1"]), find_peak("13C", e["f2"])
                if ph and pc:
                    for h in h_atoms:
                        ca = attached.get(h["atom_id"])
                        if not ca:
                            continue
                        for c in c_atoms:
                            if c["atom_id"] == ca:
                                continue
                            d = bfs_dist(ca, c["atom_id"])
                            if d is not None and d <= 3:
                                add_ev(h["atom_id"], ph, e["edge_id"], etype, src)
                                add_ev(c["atom_id"], pc, e["edge_id"], etype, src)

    for c in candidates:
        c["score"] = round(c["base_score"] + weight * len(c["evidence"]), 6)

    by_atom: Dict[str, list] = {}
    for c in candidates:
        by_atom.setdefault(c["atom_id"], []).append(c)
    for cs in by_atom.values():
        mx = max(c["score"] for c in cs)
        for c in cs:
            if mx - c["score"] <= tie_eps:
                c["status"] = "best"   # 并列最优全部保留

    # 缺失的预期 COSY 边：仅作诊断与证据标记，不排除候选。
    cosy_edges = [e for src, t, es in edge_sources if t == "COSY"
                  for e in es if e["f1"] is not None and e["f2"] is not None]

    def best_of(aid):
        return [c for c in by_atom.get(aid, []) if c["status"] == "best"]

    for b in bonds:
        a1, a2 = b["a"], b["b"]
        if a1 not in c_ids or a2 not in c_ids:
            continue
        hs1 = [h["atom_id"] for h in h_atoms if attached.get(h["atom_id"]) == a1]
        hs2 = [h["atom_id"] for h in h_atoms if attached.get(h["atom_id"]) == a2]
        for h1 in hs1:
            for h2 in hs2:
                b1, b2 = best_of(h1), best_of(h2)
                if not b1 or not b2:
                    continue
                s1, s2 = b1[0]["shift_ppm"], b2[0]["shift_ppm"]
                found = any(
                    (abs(e["f1"] - s1) <= tol and abs(e["f2"] - s2) <= tol)
                    or (abs(e["f1"] - s2) <= tol and abs(e["f2"] - s1) <= tol)
                    for e in cosy_edges)
                if not found:
                    diagnostics.append({
                        "type": "missing_edge", "expected": "COSY",
                        "atoms": [h1, h2],
                        "message": "预期耦合相关边缺失：只作为证据，不强制排除候选",
                    })
                    for c in b1 + b2:
                        if "missing-edge" not in c["flags"]:
                            c["flags"].append("missing-edge")

    # 化学位移整体偏置（按核），中位数用 NumPy 计算。
    for nuc in peaks_by_nuc:
        errs = [c["err_ppm"] for c in candidates
                if c["nucleus"] == nuc and c["status"] == "best"]
        if errs:
            bias = float(np.median(np.array(errs, dtype=float)))
            if abs(bias) > bias_thr:
                diagnostics.append({
                    "type": "global_shift_bias", "nucleus": nuc,
                    "value_ppm": round(bias, 4),
                    "message": "检测到整体化学位移偏置，可能源于参考标准/溶剂/温度变化；不静默修正",
                })
                for c in candidates:
                    if (c["nucleus"] == nuc and c["status"] == "best"
                            and "bias" not in c["flags"]):
                        c["flags"].append("bias")

    peak_users: Dict[str, Set[str]] = {}
    for c in candidates:
        peak_users.setdefault(c["peak_id"], set()).add(c["atom_id"])
    for c in candidates:
        if c["peak_id"] in overlap_group_of and "overlap" not in c["flags"]:
            c["flags"].append("overlap")
        if len(peak_users[c["peak_id"]]) > 1 and "shared-peak" not in c["flags"]:
            c["flags"].append("shared-peak")
    for grp, members in sym_groups.items():
        if len(members) > 1:
            for c in candidates:
                if c["atom_id"] in members and "symmetry" not in c["flags"]:
                    c["flags"].append("symmetry")

    for c in candidates:
        depset = {deps["mol"], nuc_dep[c["nucleus"]], params_id}
        for s in c["_srcs"]:
            depset.add(deps[s])
        c["depends_on"] = sorted(depset)
        del c["_srcs"]

    candidates.sort(key=lambda c: (c["atom_id"], -c["score"], c["candidate_id"]))
    all_deps = sorted({d for c in candidates for d in c["depends_on"]}
                      | {deps["mol"], params_id})

    return {
        "set_id": stable_id("set", fingerprint(deps), fingerprint(params)),
        "algorithm": params.get("algorithm", "assign-1.0.0"),
        "params": params, "params_id": params_id,
        "input_versions": deps, "depends_on": all_deps,
        "conditions": conditions,
        "overlap_groups": overlap_group_of,
        "candidates": candidates,
        "unknowns": unknowns,
        "diagnostics": diagnostics,
    }
