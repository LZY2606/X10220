"""候选生成与证据打分（领域计算）。

原则（机器可读取地体现在 notes/rejections/bias 中）：
- 重叠峰与缺峰不被强制唯一匹配：多个最优候选保留 tie；无峰核标记 unobserved。
- 耦合网络只作为证据：缺失预期相关边只产生 missing_expected_edge 注释，绝不拒绝候选。
- 化学位移整体偏置：以平均残差给出诊断，不擅自改写输入；是否用参考偏置修订由参数决定。
- 对称原子：等价组内候选标 symmetry_degenerate，不人为打破对称。
- 拒绝原因、误差、未知状态与候选一样是一等数据。
- 纯确定性：无随机数；并列排序按 (nucleus_id, peak_id) 稳定。
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

from . import ALGORITHM_VERSION

DEFAULT_PARAMS = {
    "version": "v1",
    "windows": {"1H": 0.5, "13C": 2.0},
    "evidence_weight": 0.5,
    "overlap_tolerance_ppm": {"1H": 0.05, "13C": 0.25},
    "bias_threshold": 0.15,
    "reference_offsets": {"1H": 0.0, "13C": 0.0},
}


def params_fingerprint(params: dict) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return "fp:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def stable_candidate_id(run_id: str, kind: str, nucleus_id: str, peak_id: str) -> str:
    basis = f"{run_id}|{kind}|{nucleus_id}|{peak_id or 'coordinate'}"
    return "cand:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _bond_neighbors(atoms: list, bonds: list, kind: str) -> dict[str, set[str]]:
    ids_by_kind = {a["id"] for a in atoms if a["kind"] == kind}
    neighbors: dict[str, set[str]] = {a: set() for a in ids_by_kind}
    for bond in bonds:
        a, b = bond["a"], bond["b"]
        if a in ids_by_kind and b in ids_by_kind:
            neighbors[a].add(b)
            neighbors[b].add(a)
    return neighbors


def expected_multiplicity(atom: dict, atoms: list, bonds: list) -> str:
    """根据相邻质子核数 (n) 推断 n+1 多重性（耦合网络证据之一）。"""
    h_neighbors = _bond_neighbors(atoms, bonds, "1H")
    n = len(h_neighbors.get(atom["id"], set()))
    names = {0: "singlet", 1: "doublet", 2: "triplet", 3: "quartet",
             4: "quintet", 5: "sextet", 6: "septet"}
    return names.get(n, "multiplet")


def _expected_edges(atoms: list, bonds: list) -> dict[tuple, list[str]]:
    """由骨架推导预期的相关边（仅当两端核均可观测到峰时有意义）。

    HSQC: 质子核与其 attached_to 碳之间（1J CH）
    COSY: 相邻质子核之间（3J HH，跨越碳链）
    HMBC: 质子核与相隔 2-3 根键的碳之间
    返回 (n1, n2) -> [实验类型...]（无序端点）
    """
    by_id = {a["id"]: a for a in atoms}
    neighbors = {a["id"]: set() for a in atoms}
    for bond in bonds:
        neighbors[bond["a"]].add(bond["b"])
        neighbors[bond["b"]].add(bond["a"])
    expected: dict[tuple, list[str]] = {}

    def add(n1: str, n2: str, etype: str) -> None:
        key = tuple(sorted((n1, n2)))
        expected.setdefault(key, [])
        if etype not in expected[key]:
            expected[key].append(etype)

    for atom in atoms:
        if atom["kind"] == "1H" and atom.get("attached_to"):
            add(atom["id"], atom["attached_to"], "HSQC")
    h_neighbors = _bond_neighbors(atoms, bonds, "1H")
    for h, nbrs in h_neighbors.items():
        for other in nbrs:
            if h < other:
                add(h, other, "COSY")
    # HMBC：从质子出发，2~3 根键到碳
    for atom in atoms:
        if atom["kind"] != "1H":
            continue
        depth2: set[str] = set()
        for n1 in neighbors[atom["id"]]:
            for n2 in neighbors[n1]:
                if n2 != atom["id"]:
                    depth2.add(n2)
        depth3: set[str] = set()
        for n2 in depth2:
            for n3 in neighbors[n2]:
                depth3.add(n3)
        for carbon in (depth2 | depth3):
            other = by_id.get(carbon)
            if other and other["kind"] == "13C" and carbon != atom.get("attached_to"):
                add(atom["id"], carbon, "HMBC")
    return expected


def _resolve_endpoint(ep: dict, peak_shift: dict) -> tuple[str | None, float | None]:
    """把边端点解析为 (peak_id 或 None, 坐标 ppm)。"""
    if ep["kind"] == "peak":
        return ep["peak_id"], peak_shift.get(ep["peak_id"])
    shift = ep.get("shift") or {}
    return None, shift.get("value")


def _observed_edge_types(nucleus_id: str, candidate_peak: str | None,
                         candidate_coord: float | None,
                         peak_coords: dict[str, float], edges: list) -> list[dict]:
    """列出与该候选相关的实际观测边（峰引用命中或坐标在容差内）。"""
    found = []
    for edge in edges:
        endpoints = []
        hit = False
        for side in ("from", "to"):
            pid, coord = _resolve_endpoint(edge[side], peak_coords)
            endpoints.append((pid, coord))
            if candidate_peak is not None and pid == candidate_peak:
                hit = True
            if candidate_coord is not None and coord is not None and abs(coord - candidate_coord) <= 0.03:
                hit = True
        if hit:
            found.append({"edge_id": edge["id"], "type": edge["type"],
                          "from": edge["from"]["raw"], "to": edge["to"]["raw"],
                          "counterpart": [c for c in endpoints]})
    return found


def compute_candidates(kind: str, list_doc: dict, skeleton_doc: dict,
                       params: dict, run_id: str) -> dict:
    """为一个核种类生成全部候选与诊断。返回运行记录（不含 run_id 之外的身份）。"""
    atoms = [a for a in skeleton_doc["atoms"] if a["kind"] == kind]
    peaks = list_doc["peaks"]
    edges = list_doc["edges"]
    window = float(params["windows"][kind])
    ew = float(params["evidence_weight"])
    overlap_tol = float(params["overlap_tolerance_ppm"][kind])
    offset = float(params.get("reference_offsets", {}).get(kind, 0.0))
    peak_coords = {p["id"]: (p["shift"] or {}).get("value") for p in peaks}
    peak_values = np.array([v for v in peak_coords.values() if v is not None])

    expected_edges = _expected_edges(skeleton_doc["atoms"], skeleton_doc["bonds"])
    equiv_map = {}
    for idx, group in enumerate(skeleton_doc.get("equiv", [])):
        for member in group:
            equiv_map[member] = idx

    candidates = []
    per_nucleus_best_residuals = {}
    for atom in atoms:
        predicted = (atom["shift"] or {}).get("value")
        nucleus_candidates = []
        for peak in peaks:
            observed = (peak["shift"] or {}).get("value")
            if predicted is None or observed is None:
                continue
            residual = observed - (predicted + offset)
            abs_res = abs(residual)
            notes = []
            rejected = False
            reject_reasons = []
            if abs_res > window:
                rejected = True
                reject_reasons.append(
                    f"outside_shift_window(|residual|={abs_res:.3f}>{window})")
            # 高斯位移契合分
            sigma = max(window / 2.0, 1e-6)
            shift_score = float(np.exp(-0.5 * (residual / sigma) ** 2))
            # 证据：观测到的预期相关边
            evidence_hits = _observed_edge_types(
                atom["id"], peak["id"], observed, peak_coords, edges)
            evidence_score = 1.0 - np.exp(-len(evidence_hits))
            score = round((1.0 - ew) * shift_score + ew * evidence_score, 6)
            # 缺失预期边：仅作证据注释
            for (n1, n2), etypes in expected_edges.items():
                if atom["id"] not in (n1, n2):
                    continue
                for etype in etypes:
                    if not any(h["type"] == etype for h in evidence_hits):
                        notes.append({"code": "missing_expected_edge",
                                      "experiment": etype,
                                      "between": [n1, n2],
                                      "detail": "correlation network is evidence only; "
                                                "missing edge never rejects a candidate"})
            # 多重性证据（不拒绝）
            exp_mult = expected_multiplicity(atom, skeleton_doc["atoms"], skeleton_doc["bonds"])
            if peak.get("multiplicity") and peak["multiplicity"] != exp_mult:
                notes.append({"code": "multiplicity_mismatch_evidence_only",
                              "observed": peak["multiplicity"], "expected": exp_mult})
            # 对称退化
            if atom["id"] in equiv_map:
                notes.append({"code": "symmetry_degenerate",
                              "equiv_group": equiv_map[atom["id"]]})
            # 重叠峰：同一峰坐标在容差内还有其它峰
            overlap_with = [p["id"] for p in peaks
                            if p["id"] != peak["id"]
                            and (p["shift"] or {}).get("value") is not None
                            and abs(p["shift"]["value"] - observed) <= overlap_tol]
            if overlap_with:
                notes.append({"code": "overlapping_peak",
                              "overlap_with": overlap_with,
                              "tolerance_ppm": overlap_tol})
            nucleus_candidates.append({
                "id": stable_candidate_id(run_id, kind, atom["id"], peak["id"]),
                "kind": kind, "nucleus_id": atom["id"], "peak_id": peak["id"],
                "coordinate_kind": "peak",
                "predicted_ppm": predicted, "observed_ppm": observed,
                "residual_ppm": round(residual, 6),
                "shift_score": round(shift_score, 6),
                "evidence_edges": evidence_hits,
                "evidence_score": round(float(evidence_score), 6),
                "score": score, "rejected": rejected,
                "reject_reasons": reject_reasons, "notes": notes,
                "status": "rejected" if rejected else "eligible",
            })
        # 坐标端点（未解析到峰的相关坐标）也作为候选证据
        for edge in edges:
            for side in ("from", "to"):
                ep = edge[side]
                if ep["kind"] != "coordinate":
                    continue
                coord = (ep.get("shift") or {}).get("value")
                if predicted is None or coord is None:
                    continue
                residual = coord - (predicted + offset)
                if abs(residual) <= window:
                    nucleus_candidates.append({
                        "id": stable_candidate_id(run_id, kind, atom["id"], f"@{coord}"),
                        "kind": kind, "nucleus_id": atom["id"], "peak_id": None,
                        "coordinate_kind": "unresolved_coordinate",
                        "predicted_ppm": predicted, "observed_ppm": coord,
                        "residual_ppm": round(residual, 6),
                        "shift_score": round(float(
                            np.exp(-0.5 * (residual / max(window / 2, 1e-6)) ** 2)), 6),
                        "evidence_edges": [{"edge_id": edge["id"], "type": edge["type"]}],
                        "evidence_score": round(float(1 - np.exp(-1)), 6),
                        "score": None, "rejected": False, "reject_reasons": [],
                        "notes": [{"code": "unresolved_coordinate_evidence_only",
                                   "endpoint": ep["raw"]}],
                        "status": "eligible",
                    })
        eligible = [c for c in nucleus_candidates if not c["rejected"]]
        eligible_sorted = sorted(eligible, key=lambda c: (-c["score"], c["peak_id"] or "~"))
        if eligible_sorted:
            top = eligible_sorted[0]["score"]
            tied = [c for c in eligible_sorted if abs(c["score"] - top) < 1e-9]
            for c in eligible_sorted:
                if abs(c["score"] - top) < 1e-9:
                    c["rank"] = 1
                    if len(tied) > 1:
                        c["notes"].append({"code": "tied_best",
                                           "tied_with": [t["id"] for t in tied
                                                         if t["id"] != c["id"]]})
                else:
                    c["rank"] = 1 + sum(1 for o in eligible_sorted if o["score"] > c["score"])
            per_nucleus_best_residuals[atom["id"]] = min(
                abs(c["residual_ppm"]) for c in eligible)
        else:
            for c in nucleus_candidates:
                if c["rejected"]:
                    c["rank"] = None
            per_nucleus_best_residuals[atom["id"]] = None
        # 无任何可观测候选 -> unobserved 一等状态
        if not eligible:
            nucleus_candidates.append({
                "id": stable_candidate_id(run_id, kind, atom["id"], "unobserved"),
                "kind": kind, "nucleus_id": atom["id"], "peak_id": None,
                "coordinate_kind": "none",
                "predicted_ppm": predicted, "observed_ppm": None,
                "residual_ppm": None, "shift_score": None,
                "evidence_edges": [], "evidence_score": None, "score": None,
                "rejected": False, "reject_reasons": [],
                "notes": [{"code": "unobserved_nucleus",
                           "detail": "no peak or coordinate within window; "
                                     "kept as unknown, never force-matched"}],
                "status": "unobserved", "rank": None,
            })
        candidates.extend(nucleus_candidates)
    # 全局偏置诊断（仅诊断，不改写）
    residuals = np.array([r for r in per_nucleus_best_residuals.values() if r is not None])
    bias = None
    if residuals.size:
        signed = []
        for atom in atoms:
            r = per_nucleus_best_residuals.get(atom["id"])
            if r is None:
                continue
            best = [c for c in candidates if c["nucleus_id"] == atom["id"]
                    and not c["rejected"] and c.get("rank") == 1]
            if best:
                signed.append(best[0]["residual_ppm"])
        if signed:
            mean = round(float(np.mean(signed)), 6)
            bias = {"mean_residual_ppm": mean,
                    "threshold": params["bias_threshold"],
                    "biased": abs(mean) >= params["bias_threshold"],
                    "code": "global_shift_bias" if abs(mean) >= params["bias_threshold"]
                            else "no_significant_bias",
                    "detail": "diagnostic only; inputs are not rewritten. "
                              "A reference offset parameter revision expires dependent candidates."}
    return {"run_id": run_id, "kind": kind, "algorithm": ALGORITHM_VERSION,
            "params_fingerprint": params_fingerprint(params),
            "params_version": params.get("version"),
            "list_content_hash": list_doc["content_hash"],
            "skeleton_content_hash": skeleton_doc["content_hash"],
            "candidates": candidates, "bias": bias}
