"""候选归属计算引擎。

原则：
- 重叠峰 / 缺峰绝不强制唯一匹配；耦合网络（COSY/HSQC/HMBC）只作证据。
- 候选、拒绝原因、未知状态都是一等数据，全部落库。
- 每个候选携带算法版本、参数指纹与直接来源（deps）。
"""
from __future__ import annotations

import numpy as np

from .domain import ALGORITHM_VERSION


def _overlap_groups(shifts, tol):
    """按 |Δδ| <= tol 做传递分组（并查集），返回 {peak_index: (gid, size)}。"""
    parent = list(range(len(shifts)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    valid = [i for i, s in enumerate(shifts) if s is not None]
    for a_pos in range(len(valid)):
        for b_pos in range(a_pos + 1, len(valid)):
            i, j = valid[a_pos], valid[b_pos]
            if abs(shifts[i] - shifts[j]) <= tol:
                pi, pj = find(i), find(j)
                if pi != pj:
                    parent[pi] = pj
    groups = {}
    for i in valid:
        groups.setdefault(find(i), []).append(i)
    out = {}
    gid = 0
    for members in groups.values():
        gid += 1
        for m in members:
            out[m] = (gid, len(members))
    return out


def compute_candidates(*, sites, peaks_h, peaks_c, edges, params):
    """纯函数：输入解析后的位点 / 峰 / 边 / 参数，输出候选与诊断。"""
    tol = float(params["tol_shift_ppm"])
    tol_c = float(params["tol_c_shift_ppm"])
    tol_int = float(params["tol_integration_rel"])
    w = params["weights"]

    proton_sites = [s for s in sites if str(s["element"]).startswith("1H")]
    carbon_expected = {s["site"]: s["expected_ppm"] for s in sites
                       if str(s["element"]).startswith("13C")}
    site_by_name = {s["site"]: s for s in proton_sites}

    sym_groups = {}
    for s in proton_sites:
        if s["sym_group"]:
            sym_groups.setdefault(s["sym_group"], []).append(s["site"])

    shifts = [p["shift_ppm"] for p in peaks_h]
    overlap_of = _overlap_groups(shifts, float(params["overlap_tol_ppm"]))

    edges_by_label = {}
    for e in edges:
        edges_by_label.setdefault(e["a_label"], []).append(e)
        edges_by_label.setdefault(e["b_label"], []).append(e)
    peak_by_label = {p["label"]: p for p in peaks_h}
    c_by_label = {p["label"]: p for p in peaks_c}

    candidates = []
    for site in proton_sites:
        group_total = None
        if site["sym_group"] and site["sym_group"] in sym_groups:
            members = sym_groups[site["sym_group"]]
            group_total = sum(site_by_name[m]["integration"] or 0 for m in members)
        for p_idx, peak in enumerate(peaks_h):
            deps = [peak["eid"], site["eid"]]
            flags, reasons, evidence = [], [], []
            if peak["shift_ppm"] is None:
                candidates.append({
                    "site": site["site"], "peak_eid": peak["eid"],
                    "score": None, "status": "unknown",
                    "flags": flags, "reasons": ["unconvertible_shift"] + [
                        d for d in (peak["diag"] or "").split(";") if d],
                    "evidence": evidence, "deps": deps})
                continue
            delta = abs(peak["shift_ppm"] - site["expected_ppm"])
            if delta > tol:
                continue
            shift_score = 1.0 - delta / tol

            integ_score = 0.0
            if peak["integration"] is not None and site["integration"]:
                options = [site["integration"]]
                if group_total:
                    options.append(group_total)
                integ_score = max(
                    max(0.0, 1.0 - abs(peak["integration"] - exp) / (exp * tol_int))
                    for exp in options if exp)
            else:
                reasons.append("integration_missing")

            mult_score = 0.0
            if peak["multiplicity_raw"] and site["multiplicity"]:
                if peak["multiplicity_raw"] == site["multiplicity"]:
                    mult_score = 1.0
                else:
                    reasons.append(
                        f"multiplicity_mismatch:{peak['multiplicity_raw']}!={site['multiplicity']}")

            ev_total = 0.0
            for nb_name in site["neighbors"]:
                nb = site_by_name.get(nb_name)
                if nb is None:
                    continue
                found = False
                for e in edges_by_label.get(peak["label"], []):
                    if e["kind"] != "COSY":
                        continue
                    other_label = e["b_label"] if e["a_label"] == peak["label"] else e["a_label"]
                    other = peak_by_label.get(other_label)
                    if other and other["shift_ppm"] is not None and \
                            abs(other["shift_ppm"] - nb["expected_ppm"]) <= tol:
                        evidence.append({"type": "COSY", "edge": e["eid"],
                                         "other_peak": other["eid"], "neighbor": nb_name})
                        deps += [e["eid"], other["eid"]]
                        ev_total += 1.0
                        found = True
                        break
                if not found:
                    reasons.append(f"missing_cosy_edge:{nb_name}")
            if site["carbon"] and site["carbon"] in carbon_expected:
                c_exp = carbon_expected[site["carbon"]]
                for e in edges_by_label.get(peak["label"], []):
                    if e["kind"] != "HSQC":
                        continue
                    other_label = e["b_label"] if e["a_label"] == peak["label"] else e["a_label"]
                    c = c_by_label.get(other_label)
                    if c and c["shift_ppm"] is not None and abs(c["shift_ppm"] - c_exp) <= tol_c:
                        evidence.append({"type": "HSQC", "edge": e["eid"],
                                         "other_peak": c["eid"], "carbon": site["carbon"]})
                        deps += [e["eid"], c["eid"]]
                        ev_total += 1.0
            for nb_name in site["neighbors"]:
                nb = site_by_name.get(nb_name)
                if nb is None or not nb["carbon"] or nb["carbon"] not in carbon_expected:
                    continue
                c_exp = carbon_expected[nb["carbon"]]
                for e in edges_by_label.get(peak["label"], []):
                    if e["kind"] != "HMBC":
                        continue
                    other_label = e["b_label"] if e["a_label"] == peak["label"] else e["a_label"]
                    c = c_by_label.get(other_label)
                    if c and c["shift_ppm"] is not None and abs(c["shift_ppm"] - c_exp) <= tol_c:
                        evidence.append({"type": "HMBC", "edge": e["eid"],
                                         "other_peak": c["eid"], "neighbor": nb_name})
                        deps += [e["eid"], c["eid"]]
                        ev_total += 0.5

            ev_score = min(1.0, ev_total / 3.0)
            score = (w["shift"] * shift_score + w["integration"] * integ_score
                     + w["multiplicity"] * mult_score + w["evidence"] * ev_score)

            gid, gsize = overlap_of.get(p_idx, (None, 1))
            if gsize > 1:
                flags.append(f"overlap_ambiguous:G{gid}")
            if site["sym_group"]:
                flags.append(f"symmetric_equivalent:{site['sym_group']}")
            status = "ambiguous" if (gsize > 1 or site["sym_group"]) else "proposed"
            candidates.append({
                "site": site["site"], "peak_eid": peak["eid"],
                "score": round(float(score), 6), "status": status,
                "flags": flags, "reasons": reasons, "evidence": evidence,
                "deps": sorted(set(deps))})

    by_site = {}
    for c in candidates:
        by_site.setdefault(c["site"], []).append(c)
    for site_name, lst in by_site.items():
        scored = sorted([c for c in lst if c["score"] is not None],
                        key=lambda c: -c["score"])
        top = scored[0]["score"] if scored else None
        tied = [c for c in scored if c["score"] == top]
        if len(tied) > 1:
            for c in tied:
                c["flags"].append("tied_best")
                c["status"] = "ambiguous"
        prev_score, prev_rank = None, 0
        for i, c in enumerate(scored):
            if c["score"] != prev_score:
                prev_rank = i + 1
                prev_score = c["score"]
            c["rank"] = prev_rank
        for c in lst:
            if c["score"] is None:
                c["rank"] = None

    diagnostics = []
    deltas = []
    for site_name, lst in by_site.items():
        scored = [c for c in lst if c["score"] is not None]
        if not scored:
            diagnostics.append({"type": "no_candidate", "site": site_name})
            continue
        best = max(scored, key=lambda c: c["score"])
        peak = next(p for p in peaks_h if p["eid"] == best["peak_eid"])
        deltas.append(peak["shift_ppm"] - site_by_name[site_name]["expected_ppm"])
    if deltas:
        med = float(np.median(np.array(deltas)))
        if abs(med) > float(params["offset_warn_ppm"]):
            diagnostics.append({"type": "global_shift_offset",
                                "median_delta_ppm": round(med, 4),
                                "threshold_ppm": params["offset_warn_ppm"]})
    return {"candidates": candidates, "diagnostics": diagnostics,
            "algorithm": ALGORITHM_VERSION}
