"""领域计算层：由原始谱峰与相关边生成可复核的归属候选。

原则：
- 重叠峰和缺峰不能被强制唯一匹配，耦合网络只用作证据；
- 候选、拒绝原因、误差、未知状态都是一等数据；
- 所有领域量携带单位；无法转换的值留在原位置并附诊断。
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

ALGO_VERSION = "candgen-1.0.0"
OVERLAP_TOL_PPM = 0.03
TIE_EPSILON = 0.05
CARBON_TOL_PPM = 2.0

UNITS = {
    "shift": "ppm",
    "intensity": "a.u.",
    "integration": "H-count",
    "frequency": "MHz",
    "temperature": "K",
}


def fnum(raw):
    """原始表示 -> float；无法转换时返回 None（调用方保留原值并记录诊断）。"""
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def params_fingerprint(algo_version: str, params: dict) -> str:
    blob = json.dumps({"algo": algo_version, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def normalize_peaks(peaks):
    """派生层规范化；原始表示完整保留在 entry['raw'] 中。"""
    out, diagnostics = [], []
    for p in peaks:
        shift = fnum(p.get("shift_ppm"))
        entry = {
            "id": p.get("id"),
            "raw": p,
            "shift_ppm": shift,
            "intensity": fnum(p.get("intensity")),
            "integration": fnum(p.get("integration")),
            "multiplicity": p.get("multiplicity"),
            "unit": "ppm",
        }
        if shift is None:
            entry["status"] = "unknown"
            diagnostics.append({
                "peak_id": p.get("id"),
                "issue": "shift_unconvertible",
                "raw_value": p.get("shift_ppm"),
                "detail": "化学位移无法转换为数值，保留原始表示，不参与评分",
            })
        else:
            entry["status"] = "ok"
        out.append(entry)
    return out, diagnostics


def overlap_groups(peaks):
    """相邻化学位移差 <= OVERLAP_TOL_PPM 的峰归为一组（只标记，不合并）。"""
    ok = [p for p in peaks if p["status"] == "ok"]
    ok.sort(key=lambda p: p["shift_ppm"])
    shifts = np.array([p["shift_ppm"] for p in ok], dtype=float)
    groups, current = [], []
    for i, p in enumerate(ok):
        if current and abs(shifts[i] - shifts[i - 1]) > OVERLAP_TOL_PPM:
            if len(current) > 1:
                groups.append([q["id"] for q in current])
            current = []
        current.append(p)
    if len(current) > 1:
        groups.append([q["id"] for q in current])
    return groups


def generate(molecule, peaks_doc, cosy_doc, hsqc_doc, hmbc_doc, params_values):
    """生成候选归属。返回的 payload 即派生结果（一等数据，可持久化、可过期）。"""
    params = dict(params_values or {})
    bias = fnum(params.get("shift_bias_ppm")) or 0.0
    fingerprint = params_fingerprint(ALGO_VERSION, params)
    param_keys = sorted(params.keys())

    peaks, diagnostics = normalize_peaks(peaks_doc.get("peaks", []))
    peak_by_id = {p["id"]: p for p in peaks}
    groups = overlap_groups(peaks)
    overlapped = {pid for g in groups for pid in g}

    sites = molecule.get("proton_sites", [])
    site_by_id = {s["id"]: s for s in sites}
    carbon_pred = {a["id"]: fnum(a.get("pred_shift_ppm")) for a in molecule.get("atoms", [])}
    coupled = {s["id"]: set() for s in sites}
    for a, b in molecule.get("couplings", []):
        coupled.setdefault(a, set()).add(b)
        coupled.setdefault(b, set()).add(a)
    expected_hmbc = molecule.get("expected_hmbc", [])

    cosy_edges = (cosy_doc or {}).get("edges", [])
    hsqc_edges = (hsqc_doc or {}).get("edges", [])
    hmbc_edges = (hmbc_doc or {}).get("edges", [])
    if cosy_doc is None:
        diagnostics.append({"issue": "missing_document", "kind": "cosy", "detail": "缺少 COSY 相关边文档，耦合证据为空"})
    if hsqc_doc is None:
        diagnostics.append({"issue": "missing_document", "kind": "hsqc", "detail": "缺少 HSQC 相关边文档"})
    if hmbc_doc is None:
        diagnostics.append({"issue": "missing_document", "kind": "hmbc", "detail": "缺少 HMBC 相关边文档"})

    candidates, rejected = [], []
    for peak in peaks:
        if peak["status"] != "ok":
            continue
        peak_cands = []
        for site in sites:
            pred = fnum(site.get("pred_shift_ppm"))
            tol = fnum(site.get("tol_ppm")) or 0.2
            if pred is None:
                continue
            expected = pred + bias
            err = peak["shift_ppm"] - expected
            if abs(err) > 2 * tol:
                rejected.append({
                    "peak_id": peak["id"],
                    "site_id": site["id"],
                    "reason": f"位移误差 {err:+.3f} ppm 超出 2×容差({tol:.2f} ppm)",
                    "shift_err_ppm": round(err, 4),
                    "unit": "ppm",
                })
                continue
            score = max(0.0, 1.0 - abs(err) / (2 * tol))
            evidence = []
            # COSY：耦合网络只用作证据，不做强制匹配
            for edge in cosy_edges:
                other = None
                if edge.get("from_peak") == peak["id"]:
                    other = edge.get("to_peak")
                elif edge.get("to_peak") == peak["id"]:
                    other = edge.get("from_peak")
                if other is None or other not in peak_by_id:
                    continue
                oshift = peak_by_id[other]["shift_ppm"]
                if oshift is None:
                    continue
                for t in coupled.get(site["id"], []):
                    tpred = fnum(site_by_id[t].get("pred_shift_ppm"))
                    ttol = fnum(site_by_id[t].get("tol_ppm")) or 0.2
                    if tpred is not None and abs(oshift - (tpred + bias)) <= 2 * ttol:
                        evidence.append({"type": "cosy", "edge_id": edge.get("id"),
                                         "other_peak": other, "supports_site": t})
                        score += 0.1
                        break
            # HSQC：峰 -> 碳位移，与相连碳的预测位移比较
            for edge in hsqc_edges:
                if edge.get("from_peak") != peak["id"]:
                    continue
                csh = fnum(edge.get("to_shift_ppm"))
                cpred = carbon_pred.get(site.get("attached_to"))
                if csh is not None and cpred is not None and abs(csh - cpred) <= CARBON_TOL_PPM:
                    evidence.append({"type": "hsqc", "edge_id": edge.get("id"),
                                     "carbon": site.get("attached_to")})
                    score += 0.2
            # HMBC：期望的远程相关
            hmbc_ok = False
            for exp in expected_hmbc:
                if exp.get("site") != site["id"]:
                    continue
                cpred = carbon_pred.get(exp.get("carbon"))
                for edge in hmbc_edges:
                    if edge.get("from_peak") != peak["id"]:
                        continue
                    csh = fnum(edge.get("to_shift_ppm"))
                    if csh is not None and cpred is not None and abs(csh - cpred) <= CARBON_TOL_PPM:
                        evidence.append({"type": "hmbc", "edge_id": edge.get("id"),
                                         "carbon": exp.get("carbon")})
                        score += 0.15
                        hmbc_ok = True
            flags = []
            if peak["id"] in overlapped:
                flags.append("overlap")
            if site.get("symmetry_group"):
                flags.append("symmetric")
            missing = []
            if coupled.get(site["id"]) and not any(e["type"] == "cosy" for e in evidence):
                missing.append("cosy")
            if any(e.get("site") == site["id"] for e in expected_hmbc) and not hmbc_ok:
                missing.append("hmbc")
            if not any(e["type"] == "hsqc" for e in evidence):
                missing.append("hsqc")
            if missing:
                flags.append("missing_correlation")
            peak_cands.append({
                "key": f"{peak['id']}->{site['id']}",
                "peak_id": peak["id"],
                "site_id": site["id"],
                "score": round(score, 4),
                "shift_err_ppm": round(err, 4),
                "unit": "ppm",
                "evidence": evidence,
                "flags": flags,
                "missing": missing,
                "equivalent_sites": list(site.get("equivalent", [])),
                "status": "candidate",
            })
        # 并列最优：与最高分差距在 TIE_EPSILON 内的候选都保留并标记
        if peak_cands:
            best = max(c["score"] for c in peak_cands)
            top = [c for c in peak_cands if best - c["score"] <= TIE_EPSILON]
            if len(top) > 1:
                for c in top:
                    if "tied" not in c["flags"]:
                        c["flags"].append("tied")
        candidates.extend(peak_cands)

    return {
        "nucleus": peaks_doc.get("nucleus", "1H"),
        "algo_version": ALGO_VERSION,
        "params_used": params,
        "params_fingerprint": fingerprint,
        "param_keys": param_keys,
        "units": UNITS,
        "context": {
            "solvent": peaks_doc.get("solvent"),
            "reference": peaks_doc.get("reference"),
            "temperature_k": peaks_doc.get("temperature_k"),
            "frequency_mhz": peaks_doc.get("frequency_mhz"),
        },
        "candidates": candidates,
        "rejected": rejected,
        "diagnostics": diagnostics,
        "overlap_groups": groups,
    }
