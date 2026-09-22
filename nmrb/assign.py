"""领域计算引擎：为每个核位点建立可复核的归属候选。

关键判定（同时作为机器可读取的发现事件）：
- OVERLAP_PEAK：重叠峰不强制唯一匹配，相关候选标记 overlap 并保留。
- SYMMETRY_EQUIVALENT：对称原子位点允许映射到同一峰，互为合法候选。
- MISSING_EXPECTED_EDGE：预期耦合边缺失，只作证据扣分，绝不伪造。
- GLOBAL_SHIFT_BIAS：残差中位数超阈时报告整体偏置；拓扑证据不受影响。
- TIED_BEST：并列最优候选全部保留，系统不自动选择。
- MISSING_PEAK / UNMATCHED_PEAK：缺峰产生 unknown 候选，多余峰报发现。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import ALGO_VERSION
from .identity import param_fingerprint, result_id
from .models import (AssignmentResult, Candidate, Conditions, Edge2D, Evidence,
                     ExpectedEdge, Finding, Peak1D, Site)

DEFAULT_PARAMS: Dict[str, Any] = {
    "shift_tolerance_ppm": {"1H": 0.25, "13C": 1.5},
    "weights": {"shift": 0.40, "integral": 0.10, "multiplicity": 0.05,
                "hsqc": 0.20, "cosy": 0.15, "hmbc": 0.10},
    "match_tolerance_ppm": {"COSY": 0.03, "HSQC": {"1H": 0.03, "13C": 0.4},
                            "HMBC": {"1H": 0.03, "13C": 0.4}},
    "overlap_window_ppm": {"1H": 0.015, "13C": 0.10},
    "bias_warn_ppm": {"1H": 0.20, "13C": 1.0},
    "score_epsilon": 1e-6,
}


def with_params(base: Optional[Dict[str, Any]] = None, **over: Any) -> Dict[str, Any]:
    import copy
    params = copy.deepcopy(base or DEFAULT_PARAMS)
    params.update(over)
    return params


def _nucleus_of(site: Site) -> str:
    # 氢标签为 '-' 的记录是碳位点；其余都是含氢位点（1H）。
    return "13C" if site.hydrogen == "-" else "1H"


def _nearest(edges: Sequence[Edge2D], axis: int, ppm: float, tol: float,
             experiment: Optional[str] = None) -> List[Edge2D]:
    out: List[Edge2D] = []
    for e in edges:
        if experiment and e.experiment != experiment:
            continue
        coord = e.f1_ppm if axis == 0 else e.f2_ppm
        if coord is None:
            continue
        if abs(coord - ppm) <= tol + 1e-9:
            out.append(e)
    return out


def _edge_partners(edge: Edge2D, axis: int) -> float:
    return edge.f2_ppm if axis == 0 else edge.f1_ppm


def _coupling_evidence(code: str, weight: float, observed: bool, detail: str,
                       source_ids: List[str], expected: bool = True) -> Evidence:
    if not expected:
        return Evidence(code, weight, 1.0, "unknown", detail, source_ids)
    if observed:
        return Evidence(code, weight, 1.0, "support", detail, source_ids)
    return Evidence(code, weight, 0.0, "absent", detail, source_ids)


def _multiplicity_support(mult: str, expected_type: str) -> float:
    head = re.split(r"[,\s]", mult.lower(), maxsplit=1)[0].strip()
    aromatic = {"d", "dd", "t", "m", "dt", "td"}
    chain = {"t", "q", "d", "m", "dd", "dt", "td", "sext"}
    if expected_type == "aromatic":
        return 0.85 if head in aromatic else 0.25
    if expected_type.startswith("exchangeable"):
        return 0.9 if head in {"br", "s", "d", "m"} else 0.3
    if expected_type == "carbonyl-quartet":
        return 0.9 if head in {"q", "m", "d"} else 0.3
    return 0.85 if head in chain else 0.35


def _site_edge_expectations(site: Site, expected: Sequence[ExpectedEdge]) -> Dict[str, List[Tuple[str, str]]]:
    """返回 {'COSY':[对端H...], 'HMBC':[碳...]}。"""
    cosy: List[Tuple[str, str]] = []
    hmbc: List[Tuple[str, str]] = []
    for ee in expected:
        if ee.kind == "bond" and (ee.a == site.hydrogen or ee.b == site.hydrogen):
            other = ee.b if ee.a == site.hydrogen else ee.a
            cosy.append((other, ee.id))
        elif ee.kind == "hmbc" and (ee.a == site.hydrogen or ee.a == site.carbon):
            target = ee.b if ee.a in (site.hydrogen, site.carbon) else ee.a
            hmbc.append((target, ee.id))
    return {"COSY": cosy, "HMBC": hmbc}


def _site_index(sites: Sequence[Site]) -> Tuple[Dict[str, Site], Dict[str, Site]]:
    return ({s.hydrogen: s for s in sites}, {s.carbon: s for s in sites})


def _integral_support(integral: Optional[float], group_size: int) -> Tuple[float, str]:
    if integral is None:
        return 0.5, "积分无法转换（原值保留），积分证据取中性 0.5"
    expected = float(group_size)
    # 积分本身带实验容差：误差 <=0.25H 视为完全支持。
    err = max(0.0, abs(integral - expected) - 0.25)
    support = math.exp(-err * 2.0)
    return support, f"积分 {integral:g} 对比对称组 {group_size} 个核（期望 ≈ {expected:g} H）"


def _overlap_groups(peaks: Sequence[Peak1D], window: float) -> Dict[str, List[str]]:
    """返回重叠簇：窗口内的峰共享同一组标记。不做唯一匹配。"""
    valid = [p for p in peaks if p.shift_ppm is not None]
    valid.sort(key=lambda p: p.shift_ppm or 0.0)
    clusters: List[List[Peak1D]] = []
    for p in valid:
        if clusters and abs((p.shift_ppm or 0) - (clusters[-1][-1].shift_ppm or 0)) <= window + 1e-12:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    mapping: Dict[str, List[str]] = {}
    for cl in clusters:
        if len(cl) > 1:
            ids = [p.id for p in cl]
            for p in cl:
                mapping[p.id] = ids
    return mapping


def _peak_deps(peak: Peak1D, edges: Sequence[Edge2D], skeleton_docs: List[str],
               peak_doc_ids: List[str]) -> List[str]:
    deps = [peak.id]
    deps += [e.id for e in edges if _touches(e, peak)]
    return deps


def _touches(edge: Edge2D, peak: Peak1D) -> bool:
    tol = 0.03 if peak.nucleus == "1H" else 0.4
    for axis, coord in ((0, edge.f1_ppm), (1, edge.f2_ppm)):
        if coord is None or peak.shift_ppm is None:
            continue
        if abs(coord - peak.shift_ppm) <= tol + 1e-9:
            nuclei = edge.nuclei
            if not nuclei or (axis < len(nuclei) and nuclei[axis] == peak.nucleus):
                return True
    return False


def build_assignment(nucleus: str, sites: Sequence[Site], peaks: Sequence[Peak1D],
                     edges: Sequence[Edge2D], expected: Sequence[ExpectedEdge],
                     params: Dict[str, Any], doc_versions: Dict[str, int]) -> AssignmentResult:
    # 索引必须基于完整骨架：1H 候选要查 13C 位点的碳位移，反之亦然。
    h_index, c_index = _site_index(sites)
    sites = [s for s in sites if _nucleus_of(s) == nucleus]
    peaks = [p for p in peaks if p.nucleus == nucleus and p.shift_ppm is not None]
    all_nucleus_peaks = [p for p in peaks]
    weights = params["weights"]
    tol_map = params["shift_tolerance_ppm"]
    ovl_window = params["overlap_window_ppm"][nucleus]
    epsilon = params["score_epsilon"]

    group_sizes: Dict[str, int] = {}
    for st in sites:
        group_sizes[st.symmetry_group] = group_sizes.get(st.symmetry_group, 0) + getattr(st, "protons", 1)

    overlap_map = _overlap_groups(peaks, ovl_window)

    # 以每组最佳残差估计整体偏置（每个对称组贡献一次）。
    residuals: List[float] = []
    for grp in {s.symmetry_group for s in sites}:
        rep = next(s for s in sites if s.symmetry_group == grp)
        if rep.expected_shift_ppm is None:
            continue
        deltas = [p.shift_ppm - rep.expected_shift_ppm for p in peaks
                  if abs(p.shift_ppm - rep.expected_shift_ppm) <= tol_map[nucleus] * 3]
        if deltas:
            residuals.append(min(deltas, key=abs))
    global_bias = float(np.median(residuals)) if residuals else 0.0
    biased = abs(global_bias) > params["bias_warn_ppm"][nucleus]
    findings: List[Finding] = []
    if biased:
        findings.append(Finding(
            "GLOBAL_SHIFT_BIAS", "warning", f"nucleus:{nucleus}",
            f"化学位移整体偏置 {global_bias:+.3f} ppm 超过阈值 "
            f"{params['bias_warn_ppm'][nucleus]:.3f} ppm；仅位移证据降级，耦合网络仍作证据。",
            related=[p.id for p in peaks]))

    candidate_map: Dict[str, List[Candidate]] = {}
    used_peaks: set[str] = set()
    input_ids: Dict[str, List[str]] = {"peaks": sorted({p.id for p in peaks})}
    edge_ids_used: set[str] = set()

    conditions = peaks[0].conditions if peaks else Conditions(nucleus=nucleus)
    cosy_match_tol = params["match_tolerance_ppm"]["COSY"] + (abs(global_bias) if biased else 0.0)
    hmbc_match_h = params["match_tolerance_ppm"]["HMBC"]["1H"] + (abs(global_bias) if biased else 0.0)
    hmbc_match_c = params["match_tolerance_ppm"]["HMBC"]["13C"]

    for site in sites:
        group_size = group_sizes[site.symmetry_group]
        within: List[Peak1D] = []
        cands: List[Candidate] = []
        for peak in peaks:
            if site.expected_shift_ppm is None:
                continue
            residual = peak.shift_ppm - site.expected_shift_ppm
            if abs(residual) > tol_map[nucleus] + 1e-9:
                continue
            within.append(peak)
            evidences: List[Evidence] = []
            # 1) 位移证据（偏置时降级而非直接否定）
            if biased:
                corrected = residual - global_bias
                shift_sup = math.exp(-(corrected ** 2) / (2 * (tol_map[nucleus] / 2) ** 2))
                evidences.append(Evidence("shift", weights["shift"], shift_sup, "support",
                    f"原始残差 {residual:+.3f} ppm；检测到整体偏置 {global_bias:+.3f} ppm，"
                    f"校正后 {corrected:+.3f} ppm（{site.expected_shift_repr}{site.expected_shift_unit}）",
                    [peak.id]))
            else:
                shift_sup = math.exp(-(residual ** 2) / (2 * (tol_map[nucleus] / 2) ** 2))
                evidences.append(Evidence("shift", weights["shift"], shift_sup, "support",
                    f"残差 {residual:+.3f} ppm，容差 ±{tol_map[nucleus]:.3f} ppm"
                    f"（预期 {site.expected_shift_repr}{site.expected_shift_unit}）", [peak.id]))
            # 2) 积分
            int_sup, int_detail = _integral_support(peak.intensity if False else peak.integral, group_size)
            evidences.append(Evidence("integral", weights["integral"], int_sup,
                "unknown" if peak.integral is None else "support", int_detail +
                f"；原始积分标记 {peak.integral_repr}{peak.integral_unit}", [peak.id]))
            # 3) 多重性
            if site.element == "carbon":
                mult_sup, mult_status, mult_detail = 0.5, "unknown", "13C 质子去耦峰恒为单峰，多重性证据不适用（中性）"
            else:
                mult_sup = _multiplicity_support(peak.multiplicity, site.element)
                mult_status = "support"
                mult_detail = f"观测多重性 {peak.multiplicity} vs 位点环境 {site.element}"
            evidences.append(Evidence("multiplicity", weights["multiplicity"], mult_sup,
                mult_status, mult_detail, [peak.id]))
            # 4) HSQC（1H↔直接键合 13C）
            hsqc_src: List[str] = []
            hsqc_ok = False
            if nucleus == "1H":
                carbon_site = c_index.get(site.carbon)
                if carbon_site is not None and carbon_site.expected_shift_ppm is not None:
                    for e in _nearest(edges, 0, peak.shift_ppm, params["match_tolerance_ppm"]["HSQC"]["1H"], "HSQC"):
                        hsqc_src.append(e.id)
                        if abs((e.f2_ppm or 0) - carbon_site.expected_shift_ppm) <= params["match_tolerance_ppm"]["HSQC"]["13C"]:
                            hsqc_ok = True
                    evidences.append(_coupling_evidence(
                        "hsqc", weights["hsqc"], hsqc_ok,
                        f"HSQC：峰 {peak.shift_repr} 与碳 {site.carbon}（预期 {carbon_site.expected_shift_repr} ppm）"
                        + ("直接相关" if hsqc_ok else "未观测到直接相关边"),
                        hsqc_src, expected=True))
                else:
                    evidences.append(Evidence("hsqc", weights["hsqc"], 0.5, "unknown",
                        f"缺少 {site.carbon} 的预期碳位移，HSQC 无法核对", []))
            else:
                h_partner = next((s for s in h_index.values()
                                  if s.carbon == site.carbon and s.hydrogen != "-"), None)
                if h_partner is not None and h_partner.expected_shift_ppm is not None:
                    for e in _nearest(edges, 1, peak.shift_ppm, params["match_tolerance_ppm"]["HSQC"]["13C"], "HSQC"):
                        hsqc_src.append(e.id)
                        if abs((e.f1_ppm or 0) - h_partner.expected_shift_ppm) <= params["match_tolerance_ppm"]["HSQC"]["1H"]:
                            hsqc_ok = True
                    evidences.append(_coupling_evidence(
                        "hsqc", weights["hsqc"], hsqc_ok,
                        f"HSQC：碳 {site.carbon} 峰 {peak.shift_repr} 与氢 {h_partner.hydrogen}"
                        + ("直接相关" if hsqc_ok else "未观测到直接相关边"),
                        hsqc_src, expected=True))
                else:
                    evidences.append(Evidence("hsqc", weights["hsqc"], 0.5, "unknown",
                        f"碳 {site.carbon} 无对应氢位点，HSQC 不适用", []))
            edge_ids_used.update(hsqc_src)
            # 5) COSY / 6) HMBC（仅 1H 有拓扑期望；13C 中性不可用）
            exps = _site_edge_expectations(site, expected)
            if nucleus == "1H":
                cosy_src: List[str] = []
                cosy_hits = 0
                for partner_h, exp_id in exps["COSY"]:
                    partner_site = next((s for s in sites if s.hydrogen == partner_h), None)
                    matched = False
                    if partner_site is not None and partner_site.expected_shift_ppm is not None:
                        for e in _nearest(edges, 0, peak.shift_ppm, cosy_match_tol, "COSY"):
                            cosy_src.append(e.id)
                            if abs((e.f2_ppm or 0) - partner_site.expected_shift_ppm) <= cosy_match_tol:
                                matched = True
                    if matched:
                        cosy_hits += 1
                    else:
                        findings.append(Finding("MISSING_EXPECTED_EDGE", "info",
                            f"{site.hydrogen}:{partner_h}",
                            f"预期 COSY 相邻 {site.hydrogen}–{partner_h} 在候选峰 {peak.shift_repr} 处未见相关边；"
                            "耦合网络只作证据，不据此唯一化。",
                            related=[peak.id, exp_id]))
                if exps["COSY"]:
                    ratio = cosy_hits / len(exps["COSY"])
                    evidences.append(Evidence("cosy", weights["cosy"], ratio,
                        "support" if cosy_hits else "absent",
                        f"COSY 命中 {cosy_hits}/{len(exps['COSY'])} 条预期相邻边",
                        sorted(set(cosy_src))))
                else:
                    evidences.append(Evidence("cosy", weights["cosy"], 0.5, "unknown",
                        "骨架未声明该氢的 COSY 相邻关系，保持中性", []))
                hmbc_src: List[str] = []
                hmbc_hits = 0
                for target_c, exp_id in exps["HMBC"]:
                    c_site = c_index.get(target_c)
                    matched = False
                    if c_site is not None and c_site.expected_shift_ppm is not None:
                        for e in _nearest(edges, 0, peak.shift_ppm, hmbc_match_h, "HMBC"):
                            hmbc_src.append(e.id)
                            if abs((e.f2_ppm or 0) - c_site.expected_shift_ppm) <= hmbc_match_c:
                                matched = True
                    if matched:
                        hmbc_hits += 1
                    else:
                        findings.append(Finding("MISSING_EXPECTED_EDGE", "info",
                            f"{site.hydrogen}:{target_c}",
                            f"预期 HMBC 远程 {site.hydrogen}→{target_c} 在候选峰处未见；保留候选，不强行排除。",
                            related=[peak.id, exp_id]))
                if exps["HMBC"]:
                    ratio = hmbc_hits / len(exps["HMBC"])
                    evidences.append(Evidence("hmbc", weights["hmbc"], ratio,
                        "support" if hmbc_hits else "absent",
                        f"HMBC 命中 {hmbc_hits}/{len(exps['HMBC'])} 条预期远程边",
                        sorted(set(hmbc_src))))
                else:
                    evidences.append(Evidence("hmbc", weights["hmbc"], 0.5, "unknown",
                        "骨架未声明该氢的 HMBC 远程关系，保持中性", []))
                edge_ids_used.update(cosy_src)
                edge_ids_used.update(hmbc_src)
            else:
                evidences.append(Evidence("cosy", weights["cosy"], 0.5, "unknown",
                    "13C 候选不使用 COSY 证据（中性）", []))
                evidences.append(Evidence("hmbc", weights["hmbc"], 0.5, "unknown",
                    "13C 候选不使用氢端 HMBC 证据（中性）", []))
            score = float(sum(ev.weight * ev.contribution for ev in evidences) /
                          sum(ev.weight for ev in evidences))
            reject: List[str] = []
            cands.append(Candidate(
                site_id=site.id, hydrogen=site.hydrogen, carbon=site.carbon,
                symmetry_group=site.symmetry_group, peak_id=peak.id,
                kind="assignment", score=score, rank=0, tied_rank=False,
                status="live", reject_reasons=reject, evidences=evidences,
                overlap=peak.id in overlap_map,
                sources={"algo": ALGO_VERSION,
                         "peak_record": peak.id,
                         "skeleton_record": site.id,
                         "edge_records": sorted({eid for ev in evidences for eid in ev.source_record_ids}),
                         "conditions": conditions.signature()}))
            used_peaks.add(peak.id)

        # 缺峰：不强制匹配到最近峰，建立 unknown 一等候选
        if not within:
            reason = ("无可转换位移的峰落在容差内" if any(p.shift_ppm is None for p in
                      [p for p in peaks] ) else f"容差 ±{tol_map[nucleus]:.3f} ppm 内无观测峰")
            cands.append(Candidate(
                site_id=site.id, hydrogen=site.hydrogen, carbon=site.carbon,
                symmetry_group=site.symmetry_group, peak_id=None,
                kind="unknown_unobserved", score=0.0, rank=0, tied_rank=False,
                status="live", reject_reasons=[],
                evidences=[Evidence("shift", weights["shift"], 0.0, "unknown",
                    f"缺峰：{reason}；该位点保持未知，不向最近峰强制唯一匹配", [site.id])],
                overlap=False,
                sources={"algo": ALGO_VERSION, "skeleton_record": site.id,
                         "peak_record": None, "conditions": conditions.signature()}))
            findings.append(Finding("MISSING_PEAK", "warning", site.hydrogen,
                f"位点 {site.hydrogen}（{site.carbon}）在 {nucleus} 谱中缺峰，保留 unknown 候选。",
                related=[site.id]))

        # 排名（含并列判定）
        ranked = sorted(cands, key=lambda c: -c.score)
        scores = [c.score for c in ranked]
        for idx, cand in enumerate(ranked):
            cand.rank = idx + 1
            cand.tied_rank = any(abs(cand.score - s) <= epsilon for j, s in enumerate(scores) if j != idx)
        best = ranked[0] if ranked else None
        if best and best.kind == "assignment" and best.tied_rank:
            tied_peaks = [c.peak_id for c in ranked if abs(c.score - best.score) <= epsilon]
            findings.append(Finding("TIED_BEST", "warning", site.hydrogen,
                f"位点 {site.hydrogen} 出现并列最优候选（{len(tied_peaks)} 个），全部保留，系统不自动选择。",
                related=tied_peaks))
        if best and best.overlap:
            findings.append(Finding("OVERLAP_PEAK", "warning", site.hydrogen,
                f"位点 {site.hydrogen} 的候选峰处于重叠窗 {ovl_window:g} ppm 内，"
                "不强制唯一匹配；重叠峰可同时作为对称/多位点候选。",
                related=overlap_map.get(best.peak_id, [best.peak_id])))
        candidate_map[site.id] = ranked
        input_ids["edges"] = sorted(edge_ids_used)

    # 对称原子：同组多个位点允许共享峰，属于合法候选而非歧义错误
    for grp, size in group_sizes.items():
        if size > 1:
            members = [s.hydrogen for s in sites if s.symmetry_group == grp]
            findings.append(Finding("SYMMETRY_EQUIVALENT", "info", grp,
                f"对称组 {grp} 含 {size} 个等价位点（{', '.join(members)}）；"
                "它们可映射同一峰，互为合法候选，不做强制区分。",
                related=[s.id for s in sites if s.symmetry_group == grp]))

    # 未被任何位点候选使用的可转换峰
    matched = {pid for cands in candidate_map.values() for c in cands if c.peak_id for pid in [c.peak_id]}
    for peak in peaks:
        if peak.id not in matched:
            findings.append(Finding("UNMATCHED_PEAK", "info", peak.id,
                f"峰 {peak.shift_repr} ppm 未落入任何位点容差；保留在峰表，不删除。",
                related=[peak.id]))

    fp = param_fingerprint(params)
    inputs = {"peaks": sorted({p.id for p in peaks}),
              "edges": sorted({e.id for e in edges}),
              "sites": sorted({s.id for s in sites})}
    rid = result_id(nucleus, inputs, ALGO_VERSION, fp)
    return AssignmentResult(
        id=rid, nucleus=nucleus, algo_version=ALGO_VERSION, param_fingerprint=fp,
        params=params, conditions_signature=conditions.signature(),
        candidate_map=candidate_map, findings=findings,
        input_record_ids=inputs, doc_versions=doc_versions,
        global_bias_ppm=(global_bias if biased else None))


def run_for_both_nuclei(sites: Sequence[Site], peaks_all: Sequence[Peak1D],
                        edges: Sequence[Edge2D], expected: Sequence[ExpectedEdge],
                        params: Dict[str, Any], doc_versions: Dict[str, int]
                        ) -> Dict[str, AssignmentResult]:
    out: Dict[str, AssignmentResult] = {}
    for nucleus in ("1H", "13C"):
        out[nucleus] = build_assignment(nucleus, sites, peaks_all, edges, expected,
                                        params, doc_versions)
    return out
