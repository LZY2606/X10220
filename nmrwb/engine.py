"""归属候选引擎（算法版本 assign-1.0.0）。

规则：
- 重叠峰与缺峰绝不强制唯一匹配；每个核保留全部合法候选（>= KEEP_THRESHOLD）；
- 耦合网络（COSY/HSQC/HMBC）只作为加分证据，缺失相关边只记录诊断，不否决；
- 并列最优（|score 差| <= TIE_EPS）全部标记 tied；
- 无候选时写入 peak_key=NULL 的 unknown 记录；
- 每个候选携带算法版本、参数指纹与直接来源（峰/边/参数/分子版本）。
"""
import hashlib
import json

import numpy as np

from .store import MAIN_BRANCH

ALGO_VERSION = "assign-1.0.0"
PARAM_FIELDS_USED = ["solvent", "reference", "freq_mhz"]  # 温度不进模型
TOL = {"1H": 0.08, "13C": 0.6}          # ppm，坐标基准：化学位移 ppm
KEEP_THRESHOLD = 0.35
TIE_EPS = 0.02
BONUS = {"HSQC": 0.10, "COSY": 0.08, "HMBC": 0.06}
BONUS_CAP = 0.30


def params_fingerprint(params):
    subset = {k: params.get(k) for k in PARAM_FIELDS_USED}
    blob = json.dumps(subset, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _gauss(delta, tol):
    return float(np.exp(-((delta / tol) ** 2)))


def _overlap_groups(peaks):
    """同核素峰按化学位移排序，相邻差 <= tol 归为一组重叠。"""
    groups = []
    by_nuc = {}
    for p in peaks:
        by_nuc.setdefault(p["nucleus"], []).append(p)
    for nuc, ps in by_nuc.items():
        ps = sorted((p for p in ps if p["shift_ppm"] is not None),
                    key=lambda p: p["shift_ppm"])
        cur = []
        for p in ps:
            if cur and abs(p["shift_ppm"] - cur[-1]["shift_ppm"]) > TOL[nuc]:
                if len(cur) > 1:
                    groups.append((nuc, list(cur)))
                cur = []
            cur.append(p)
        if len(cur) > 1:
            groups.append((nuc, list(cur)))
    return groups


def run_compute(store, branch_id=MAIN_BRANCH, actor="system"):
    heads = store.head_inputs()
    peaks_inputs = [r for r in heads if r["kind"] == "peaks1d"]
    corr_inputs = [r for r in heads if r["kind"] == "corr2d"]
    params_inputs = [r for r in heads if r["kind"] == "params"]
    mol_inputs = [r for r in heads if r["kind"] == "molecule"]
    if not (peaks_inputs and params_inputs and mol_inputs):
        return store.emit("compute.skipped", {
            "reason": "need molecule + params + peaks1d inputs"}, branch_id, actor)

    params_input = max(params_inputs, key=lambda r: r["seq"])
    mol_input = max(mol_inputs, key=lambda r: r["seq"])
    params = store.params_of(params_input["input_id"])
    atoms = store.atoms_of(mol_input["input_id"])
    bonds = store.bonds_of(mol_input["input_id"])
    peaks, edges = [], []
    for pi in peaks_inputs:
        peaks += store.peaks_of(pi["input_id"])
    for ci in corr_inputs:
        edges += store.edges_of(ci["input_id"])
    # 无法转换的峰留在库中（UI 可见、带诊断），但不参与匹配
    matchable = [p for p in peaks if p["shift_ppm"] is not None]
    by_local = {p["local_id"]: p for p in matchable}
    atom_by_id = {a["atom_id"]: a for a in atoms}
    fp = params_fingerprint(params)
    run_id = store.new_id("run")

    # 碳骨架邻接（HMBC 证据用，键数距离）
    adj = {}
    for a, b in bonds:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    def c_within(c_id, maxd):
        seen, frontier = {c_id}, {c_id}
        for _ in range(maxd):
            nxt = set()
            for c in frontier:
                nxt |= adj.get(c, set())
            nxt -= seen
            seen |= nxt
            frontier = nxt
        return seen

    def h_attached(c_id):
        return [a for a in atoms
                if a["element"] == "H" and a["attached"] == c_id]

    def evidence_for(atom, peak):
        """返回 (evidence_list, bonus, extra_peak_keys, edge_keys)。"""
        ev, bonus, src_peaks, src_edges = [], 0.0, set(), set()
        for e in edges:
            if e["a"] == peak["local_id"]:
                other_local = e["b"]
            elif e["b"] == peak["local_id"]:
                other_local = e["a"]
            else:
                continue
            other = by_local.get(other_local)
            if not other:
                continue
            ok = False
            if e["kind"] == "HSQC":
                if atom["element"] == "H" and other["nucleus"] == "13C" \
                        and atom["attached"]:
                    cat = atom_by_id.get(atom["attached"])
                    if cat and cat["c_shift"] is not None and \
                            abs(other["shift_ppm"] - cat["c_shift"]) <= TOL["13C"]:
                        ok = True
                elif atom["element"] == "C" and other["nucleus"] == "1H":
                    for h in h_attached(atom["atom_id"]):
                        if h["h_shift"] is not None and \
                                abs(other["shift_ppm"] - h["h_shift"]) <= TOL["1H"]:
                            ok = True
            elif e["kind"] == "COSY" and atom["element"] == "H" \
                    and other["nucleus"] == "1H" and atom["attached"]:
                for nb_c in c_within(atom["attached"], 1):
                    for h in h_attached(nb_c):
                        if h["atom_id"] == atom["atom_id"]:
                            continue
                        if h["h_shift"] is not None and \
                                abs(other["shift_ppm"] - h["h_shift"]) <= TOL["1H"]:
                            ok = True
            elif e["kind"] == "HMBC" and atom["element"] == "H" \
                    and other["nucleus"] == "13C" and atom["attached"]:
                for nb_c in c_within(atom["attached"], 3):
                    cat = atom_by_id.get(nb_c)
                    if cat and cat["c_shift"] is not None and \
                            abs(other["shift_ppm"] - cat["c_shift"]) <= TOL["13C"]:
                        ok = True
            if ok:
                ev.append({"type": e["kind"].lower() + "_support",
                           "edge": e["edge_key"], "peak": other["peak_key"]})
                bonus += BONUS[e["kind"]]
                src_edges.add(e["edge_key"])
                src_peaks.add(other["peak_key"])
        return ev, min(bonus, BONUS_CAP), src_peaks, src_edges

    def missing_edges(atom, peak):
        """H 核：附着碳有对应 13C 峰、但缺少连接二者的 HSQC 边。"""
        if atom["element"] != "H" or not atom["attached"]:
            return []
        cat = atom_by_id.get(atom["attached"])
        if not cat or cat["c_shift"] is None:
            return []
        partners = [p for p in matchable if p["nucleus"] == "13C"
                    and abs(p["shift_ppm"] - cat["c_shift"]) <= TOL["13C"]]
        if not partners:
            return []
        linked = {e["b"] if e["a"] == peak["local_id"] else e["a"]
                  for e in edges if e["kind"] == "HSQC"
                  and peak["local_id"] in (e["a"], e["b"])}
        miss = [p["peak_key"] for p in partners
                if p["local_id"] not in linked]
        return [{"expected_hsqc_to": mk, "attached_c": cat["atom_id"]}
                for mk in miss]

    created, missing_report, tie_report = [], [], []
    for atom in atoms:
        nuc = "1H" if atom["element"] == "H" else "13C"
        expected = atom["h_shift"] if atom["element"] == "H" else atom["c_shift"]
        atom_cands = []
        if expected is not None:
            for p in matchable:
                if p["nucleus"] != nuc:
                    continue
                shift_term = _gauss(p["shift_ppm"] - expected, TOL[nuc])
                if nuc == "1H":
                    if p["integral"] is not None and atom["nh"]:
                        integ_term = _gauss(p["integral"] - atom["nh"], 0.6)
                    else:
                        integ_term = 0.5
                    mult_term = 1.0 if (atom["mult"] and
                                        p["multiplicity"] == atom["mult"]) else 0.6
                else:
                    integ_term = mult_term = 1.0
                ev, bonus, src_peaks, src_edges = evidence_for(atom, p)
                miss = missing_edges(atom, p)
                for m in miss:
                    ev.append({"type": "missing_hsqc_edge", **m})
                    missing_report.append({"atom": atom["atom_id"],
                                           "peak": p["peak_key"], **m})
                score = min(1.0, 0.62 * shift_term + 0.20 * integ_term
                            + 0.08 * mult_term + bonus)
                if score < KEEP_THRESHOLD:
                    continue
                sources = {
                    "peaks": sorted({p["peak_key"]} | src_peaks),
                    "edges": sorted(src_edges),
                    "params": params_input["input_id"],
                    "molecule": mol_input["input_id"],
                }
                atom_cands.append({
                    "run_id": run_id, "nucleus": atom["atom_id"],
                    "peak_key": p["peak_key"], "score": round(score, 6),
                    "evidence": json.dumps(ev, ensure_ascii=False),
                    "algo_version": ALGO_VERSION, "params_fp": fp,
                    "param_fields": json.dumps(PARAM_FIELDS_USED),
                    "sources": json.dumps(sources, ensure_ascii=False),
                })
        if not atom_cands:
            atom_cands.append({
                "run_id": run_id, "nucleus": atom["atom_id"],
                "peak_key": None, "score": None,
                "evidence": json.dumps([{"type": "unknown",
                                         "reason": "no peak within tolerance"}]),
                "algo_version": ALGO_VERSION, "params_fp": fp,
                "param_fields": json.dumps(PARAM_FIELDS_USED),
                "sources": json.dumps({"peaks": [], "edges": [],
                                       "params": params_input["input_id"],
                                       "molecule": mol_input["input_id"]}),
                "_unknown": True,
            })
        best = max((c["score"] or 0.0) for c in atom_cands)
        top = [c for c in atom_cands
               if c["score"] is not None and best - c["score"] <= TIE_EPS]
        tied = len(top) > 1
        for c in atom_cands:
            if c.get("_unknown"):
                c["base_status"] = "unknown"
            elif tied and best - c["score"] <= TIE_EPS:
                c["base_status"] = "tied"
            else:
                c["base_status"] = "candidate"
            c.pop("_unknown", None)
            row = store.add_candidate(c)
            created.append(row)
            if c["base_status"] == "tied":
                tie_report.append(row["cand_id"])

    # ---- 机器可读的判定事件 ----
    for nuc, group in _overlap_groups(matchable):
        store.emit("judgment.overlap_not_forced", {
            "nucleus": nuc,
            "peaks": [p["peak_key"] for p in group],
            "shifts_ppm": [p["shift_ppm"] for p in group],
            "rule": "重叠峰/缺峰不强制唯一匹配，全部合法候选保留",
        }, branch_id, actor)
    if missing_report:
        store.emit("evidence.missing_edge", {
            "items": missing_report,
            "rule": "缺失相关边只降低证据分，不否决候选",
        }, branch_id, actor)
    used_edges = sorted({ek for c in created
                         for ek in json.loads(c["sources"])["edges"]})
    store.emit("judgment.coupling_evidence_only", {
        "edges_used": used_edges,
        "rule": "耦合网络只用作证据加分，不作为唯一匹配约束",
    }, branch_id, actor)
    sym_groups = {}
    for a in atoms:
        if a["sym_group"]:
            sym_groups.setdefault(a["sym_group"], []).append(a["atom_id"])
    for g, members in sym_groups.items():
        if len(members) < 2:
            continue
        peak_sets = {m: sorted(c["peak_key"] for c in created
                               if c["nucleus"] == m) for m in members}
        store.emit("symmetry.shared_candidates", {
            "group": g, "atoms": members, "peak_sets": peak_sets,
            "rule": "对称原子共享候选集合，不强制区分",
        }, branch_id, actor)
    if tie_report:
        store.emit("candidates.tied", {
            "cand_ids": tie_report,
            "rule": "并列最优候选全部保留为 tied",
        }, branch_id, actor)
    summary = {
        "run_id": run_id, "algo_version": ALGO_VERSION, "params_fp": fp,
        "candidates": len(created),
        "tied": sum(1 for c in created if c["base_status"] == "tied"),
        "unknown": sum(1 for c in created if c["base_status"] == "unknown"),
    }
    store.emit("compute.run", summary, branch_id, actor)
    return summary
