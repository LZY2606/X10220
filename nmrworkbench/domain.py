"""领域计算与状态投影。

候选生成规则（algo_version = "candgen/1.0.0"）：
- 重叠峰与缺峰绝不强制唯一匹配；全部合法候选都保留。
- 耦合网络（COSY/HSQC/HMBC 相关边）只作为证据调整分数，不作为硬约束。
- 候选、拒绝原因、误差与未知状态都是一等数据。
- 每个候选记录 depends_on_streams / depends_on_params，局部输入变化只使依赖它的结果过期。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Optional

import numpy as np

from .units import normalize_frequency, normalize_shift, normalize_temperature

ALGO_VERSION = "candgen/1.0.0"
SCORE_THRESHOLD = 0.30
TIE_EPSILON = 1e-6

STREAMS = ("peaks_1h", "peaks_13c", "edges_cosy", "edges_hsqc", "edges_hmbc", "skeleton", "conditions")


def param_fingerprint(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def candidate_id(kind: str, target: str, peak_id: str) -> str:
    digest = hashlib.sha256(f"{kind}|{target}|{peak_id}".encode()).hexdigest()[:8]
    return f"cand-{digest}"


# ---------------------------------------------------------------- 候选生成

def _norm_peak(peak: dict, field_mhz: Optional[float]) -> tuple[Optional[float], list[dict]]:
    res = normalize_shift(peak["shift"], field_mhz)
    return res["canonical"], res["diagnostics"]


def _overlap_groups(peaks: list[dict], shifts: dict[str, Optional[float]], tol: float) -> dict[str, str]:
    """返回 peak_id -> 重叠组 id（仅大小>1 的组）。"""
    ordered = sorted((p["peak_id"] for p in peaks if shifts.get(p["peak_id"]) is not None), key=lambda pid: shifts[pid])
    groups: dict[str, list[str]] = {}
    for pid in ordered:
        placed = False
        for gid, members in groups.items():
            if abs(shifts[pid] - shifts[members[-1]]) <= tol:
                members.append(pid)
                placed = True
                break
        if not placed:
            groups[f"ov-{len(groups) + 1}"] = [pid]
    return {pid: gid for gid, members in groups.items() if len(members) > 1 for pid in members}


def generate_candidates(inputs: dict[str, dict], params: dict) -> dict:
    """确定性候选生成。inputs: {stream: {"version_id":..., "data":...}}。"""
    diagnostics: list[dict] = []
    skeleton = inputs["skeleton"]["data"]
    conditions = inputs["conditions"]["data"]
    freq = normalize_frequency(conditions["field"])
    diagnostics += freq["diagnostics"]
    temp = normalize_temperature(conditions["temperature"])
    diagnostics += temp["diagnostics"]
    field_mhz = freq["canonical"]

    h_peaks = inputs["peaks_1h"]["data"]["peaks"]
    c_peaks = inputs["peaks_13c"]["data"]["peaks"]
    h_shift: dict[str, Optional[float]] = {}
    c_shift: dict[str, Optional[float]] = {}
    for p in h_peaks:
        h_shift[p["peak_id"]], diags = _norm_peak(p, field_mhz)
        diagnostics += [{**d, "peak_id": p["peak_id"]} for d in diags]
    for p in c_peaks:
        c_shift[p["peak_id"]], diags = _norm_peak(p, field_mhz)
        diagnostics += [{**d, "peak_id": p["peak_id"]} for d in diags]

    cosy_edges = inputs["edges_cosy"]["data"]["edges"]
    hsqc_edges = inputs["edges_hsqc"]["data"]["edges"]
    hmbc_edges = inputs["edges_hmbc"]["data"]["edges"]
    present_edge_ids = {e["edge_id"] for e in cosy_edges + hsqc_edges + hmbc_edges}

    tol = float(params["shift_tolerance_ppm"])
    overlap_tol = float(params["overlap_tolerance_ppm"])
    overlap_of = _overlap_groups(h_peaks, h_shift, overlap_tol)

    w = params["weights"]
    candidates: list[dict] = []

    def shift_component(value: Optional[float], rng: list[float]) -> tuple[Optional[float], Optional[str]]:
        if value is None:
            return None, "shift.unknown"
        lo, hi = rng
        if lo <= value <= hi:
            return 1.0, None
        dist = min(abs(value - lo), abs(value - hi))
        return math.exp(-((dist / tol) ** 2)), None

    # ---- 质子位点候选（对称原子逐原子生成，天然并列）----
    for site in skeleton["proton_sites"]:
        expected_edges = site.get("expected_edges", [])
        present = [e for e in expected_edges if e in present_edge_ids]
        missing = [e for e in expected_edges if e not in present_edge_ids]
        # 耦合网络只作证据：缺边只降低证据分，不过滤候选
        edge_score = len(present) / len(expected_edges) if expected_edges else 1.0
        for atom in site["atom_ids"]:
            for peak in h_peaks:
                pid = peak["peak_id"]
                s_shift, unknown = shift_component(h_shift.get(pid), site["expected_range_ppm"])
                integ = peak["integration"]["value"]
                s_int = max(0.0, 1.0 - abs(integ - site["proton_count"]) / max(site["proton_count"], 1e-9))
                s_mult = 1.0 if peak.get("multiplicity") == site["expected_multiplicity"] else 0.3
                components = {
                    "shift": s_shift,
                    "integration": s_int,
                    "multiplicity": s_mult,
                    "correlation_evidence": edge_score,
                }
                known = {k: v for k, v in components.items() if v is not None}
                weights = {"shift": w["shift"], "integration": w["integration"],
                           "multiplicity": w["multiplicity"], "correlation_evidence": w["correlation"]}
                wsum = sum(weights[k] for k in known)
                score = sum(known[k] * weights[k] for k in known) / wsum if wsum else 0.0
                if score < SCORE_THRESHOLD:
                    continue
                depends_streams = ["peaks_1h", "skeleton"]
                if expected_edges:
                    depends_streams.append("edges_cosy")
                if h_shift.get(pid) is not None:
                    depends_streams.append("conditions")
                cand = {
                    "candidate_id": candidate_id("1H", atom, pid),
                    "kind": "1H",
                    "atom_id": atom,
                    "site": site["label"],
                    "peak_id": pid,
                    "score": round(score, 6),
                    "components": {k: (round(v, 6) if v is not None else None) for k, v in components.items()},
                    "unknown": ([unknown] if unknown else []),
                    "evidence": {"present_edges": present, "missing_edges": missing},
                    "flags": {
                        "overlap_group": overlap_of.get(pid),
                        "symmetry_degenerate": len(site["atom_ids"]) > 1,
                        "missing_evidence": bool(missing),
                    },
                    "depends_on_streams": sorted(set(depends_streams)),
                    "depends_on_params": ["shift_tolerance_ppm", "overlap_tolerance_ppm", "weights"],
                }
                candidates.append(cand)

    # ---- 碳候选（HSQC 直连 + HMBC 证据）----
    hsqc_by_h: dict[str, list[str]] = {}
    for e in hsqc_edges:
        hsqc_by_h.setdefault(e["from_peak"], []).append(e["to_peak"])
    for catom in skeleton["carbon_sites"]:
        expected_edges = catom.get("expected_edges", [])
        present = [e for e in expected_edges if e in present_edge_ids]
        missing = [e for e in expected_edges if e not in present_edge_ids]
        edge_score = len(present) / len(expected_edges) if expected_edges else 0.5
        for peak in c_peaks:
            pid = peak["peak_id"]
            s_shift, unknown = shift_component(c_shift.get(pid), catom["expected_range_ppm"])
            components = {"shift": s_shift, "correlation_evidence": edge_score}
            known = {k: v for k, v in components.items() if v is not None}
            weights = {"shift": w["shift"], "correlation_evidence": w["correlation"]}
            wsum = sum(weights[k] for k in known)
            score = sum(known[k] * weights[k] for k in known) / wsum if wsum else 0.0
            if score < SCORE_THRESHOLD:
                continue
            depends_streams = ["peaks_13c", "skeleton", "edges_hsqc", "edges_hmbc"]
            if c_shift.get(pid) is not None:
                depends_streams.append("conditions")
            candidates.append({
                "candidate_id": candidate_id("13C", catom["atom_id"], pid),
                "kind": "13C",
                "atom_id": catom["atom_id"],
                "site": catom["atom_id"],
                "peak_id": pid,
                "score": round(score, 6),
                "components": {k: (round(v, 6) if v is not None else None) for k, v in components.items()},
                "unknown": ([unknown] if unknown else []),
                "evidence": {"present_edges": present, "missing_edges": missing},
                "flags": {
                    "overlap_group": None,
                    "symmetry_degenerate": catom.get("symmetry_group") is not None,
                    "missing_evidence": bool(missing),
                },
                "depends_on_streams": sorted(set(depends_streams)),
                "depends_on_params": ["shift_tolerance_ppm", "weights"],
            })

    # ---- 并列最优标记：同一归属目标下分数并列的候选都保留 ----
    by_target: dict[str, list[dict]] = {}
    for c in candidates:
        by_target.setdefault(c["atom_id"], []).append(c)
    for target, cands in by_target.items():
        best = max(c["score"] for c in cands)
        tied = [c for c in cands if abs(c["score"] - best) <= TIE_EPSILON]
        for c in cands:
            c["flags"]["tied_best"] = len(tied) > 1 and c in tied

    candidates.sort(key=lambda c: (c["atom_id"], -c["score"], c["peak_id"]))
    sources = {s: inputs[s]["version_id"] for s in STREAMS if s in inputs}
    return {
        "algo_version": ALGO_VERSION,
        "param_fingerprint": param_fingerprint(params),
        "params_snapshot": params,
        "sources": sources,
        "candidates": candidates,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------- 状态投影

def empty_state() -> dict:
    return {
        "project": None,
        "inputs": {},
        "input_history": {},
        "params": None,
        "param_history": [],
        "candidate_sets": [],
        "decisions": {},
        "judgments": [],
        "diagnostics": [],
        "events_seen": 0,
    }


def project(events: list[dict]) -> dict:
    """把事件序列折叠成可见状态。只折叠传入的事件 → 重放任意历史点。"""
    state = empty_state()
    for e in events:
        k = e["kind"]
        p = e["payload"]
        state["events_seen"] += 1
        if k == "project_created":
            state["project"] = {**p, "created_event": e["event_id"]}
        elif k in ("input_committed", "input_revised"):
            stream = p["stream"]
            version = {
                "version_id": p["version_id"],
                "parent_version_id": p.get("parent_version_id"),
                "data": p["data"],
                "source_file": p.get("source_file"),
                "committed_by": e["actor"],
                "committed_event": e["event_id"],
                "virtual_time": e["virtual_time"],
            }
            state["inputs"][stream] = version
            state["input_history"].setdefault(stream, []).append(
                {kk: version[kk] for kk in ("version_id", "parent_version_id", "committed_by", "committed_event", "virtual_time")}
            )
        elif k in ("params_committed", "params_revised"):
            state["params"] = p["params"]
            state["param_history"].append({
                "params": p["params"],
                "changed_keys": p.get("changed_keys", []),
                "fingerprint": e["param_fingerprint"],
                "event_id": e["event_id"],
                "actor": e["actor"],
            })
        elif k == "candidates_generated":
            state["candidate_sets"].append({
                "set_id": p["set_id"],
                "event_id": e["event_id"],
                "algo_version": e["algo_version"],
                "param_fingerprint": e["param_fingerprint"],
                "params_snapshot": p["params_snapshot"],
                "sources": p["sources"],
                "candidates": p["candidates"],
                "diagnostics": p.get("diagnostics", []),
                "branch": e["branch"],
            })
        elif k == "decision_made":
            state["decisions"][p["decision_id"]] = {
                **p, "event_id": e["event_id"], "branch": e["branch"],
                "actor": e["actor"], "virtual_time": e["virtual_time"], "reverted": False,
            }
        elif k == "decision_reverted":
            target = state["decisions"].get(p["decision_id"])
            if target is not None:
                target["reverted"] = True
                target["reverted_by_event"] = e["event_id"]
        elif k == "conflict_recorded":
            state.setdefault("conflicts", {})[p["conflict_id"]] = {**p, "event_id": e["event_id"], "resolved": False}
        elif k == "conflict_resolved":
            conf = state.setdefault("conflicts", {}).get(p["conflict_id"])
            if conf is not None:
                conf["resolved"] = True
                conf["resolution"] = p
        elif k == "judgment_recorded":
            state["judgments"].append({**p, "event_id": e["event_id"], "branch": e["branch"], "actor": e["actor"]})
        elif k == "diagnostic_emitted":
            state["diagnostics"].append({**p, "event_id": e["event_id"]})
    _annotate_candidate_status(state)
    return state


def _annotate_candidate_status(state: dict) -> None:
    """状态 = f(当前位置)：过期结果仍可查看，但不能冒充当前结果。"""
    current_versions = {s: v["version_id"] for s, v in state["inputs"].items()}
    params = state["params"] or {}
    latest_idx = len(state["candidate_sets"]) - 1
    for idx, cset in enumerate(state["candidate_sets"]):
        for cand in cset["candidates"]:
            stale_reasons = []
            for stream in cand["depends_on_streams"]:
                if stream in current_versions and cset["sources"].get(stream) != current_versions[stream]:
                    stale_reasons.append(f"输入流 {stream} 已修订（{cset['sources'].get(stream)} → {current_versions[stream]}）")
            snap = cset["params_snapshot"]
            for key in cand["depends_on_params"]:
                if key in snap and key in params and snap[key] != params[key]:
                    stale_reasons.append(f"参数 {key} 已修订")
            if idx < latest_idx:
                cand["status"] = "superseded"
            elif stale_reasons:
                cand["status"] = "stale"
            else:
                cand["status"] = "current"
            cand["stale_reasons"] = stale_reasons


def effective_assignments(state: dict) -> dict:
    """可见结论：未被撤销的确认决定。"""
    out = {}
    for d in state["decisions"].values():
        if d["verdict"] == "confirm" and not d["reverted"]:
            out[d["candidate_id"]] = d
    return out
