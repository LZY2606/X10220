"""归属候选引擎（assign-1.0.0）。

原则：
- 重叠峰与缺峰不强制唯一匹配；耦合网络（COSY/HSQC/HMBC）只作为证据加权，不作硬约束。
- 候选、拒绝原因、误差、未知状态都是一等数据。
- 输出完全确定：排序规则固定，不依赖随机数。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

ALGORITHM_VERSION = "assign-1.0.0"

DEFAULT_WEIGHTS = {
    "shift": 1.0,
    "integration": 0.5,
    "multiplicity": 0.3,
    "hsqc": 0.6,
    "cosy": 0.4,
    "hmbc": 0.4,
}


def param_fingerprint(params: dict[str, Any]) -> str:
    canon = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _window(center: float, tol: float) -> tuple[float, float]:
    return (center - tol, center + tol)


def _in_window(value: float | None, center: float, tol: float) -> bool:
    if value is None:
        return False
    lo, hi = _window(center, tol)
    return lo <= value <= hi


def _build_targets(skeleton_atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按对称组归并骨架原子，生成 1H / 13C 归属目标。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for atom in skeleton_atoms:
        key = atom["symmetry_group"] or atom["atom_id"]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(atom)

    targets: list[dict[str, Any]] = []
    for key in order:
        atoms = groups[key]
        rep = atoms[0]
        sym = rep["symmetry_group"]
        h_total = sum(a["h_count"]["value"] or 0 for a in atoms)
        if rep["element"] == "C":
            targets.append({
                "target_id": f"tg-13C-{key}",
                "kind": "13C",
                "atoms": [a["atom_id"] for a in atoms],
                "labels": [a["label"] for a in atoms],
                "symmetry_group": sym,
                "expected_ppm": rep["expected_shift"]["value"],
                "expected": rep["expected_shift"],
                "h_count": 0,
                "multiplicity_expected": None,
                "attached_carbon": None,
            })
        if h_total > 0:
            attached = f"tg-13C-{key}" if rep["element"] == "C" else None
            h_expected = (rep["expected_shift"] if rep["element"] == "H"
                          else rep["h_expected_shift"])
            targets.append({
                "target_id": f"tg-1H-{key}",
                "kind": "1H",
                "atoms": [a["atom_id"] for a in atoms],
                "labels": [a["label"] for a in atoms],
                "symmetry_group": sym,
                "expected_ppm": h_expected["value"],
                "expected": h_expected,
                "h_count": h_total,
                "multiplicity_expected": rep["multiplicity_expected"]["value"],
                "attached_carbon": attached,
            })
    return targets


def _bond_distance(bonds: list[dict[str, Any]], start: str, goal: str, limit: int = 4) -> int | None:
    adj: dict[str, list[str]] = {}
    for b in bonds:
        adj.setdefault(b["a"], []).append(b["b"])
        adj.setdefault(b["b"], []).append(b["a"])
    frontier = [(start, 0)]
    seen = {start}
    while frontier:
        node, dist = frontier.pop(0)
        if node == goal:
            return dist
        if dist >= limit:
            continue
        for nxt in adj.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((nxt, dist + 1))
    return None


def compute_candidates(
    skeleton: dict[str, Any],
    h1_peaks: list[dict[str, Any]],
    c13_peaks: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    tol_h = float(params["shift_tolerance_ppm"])
    tol_c = float(params.get("c13_tolerance_ppm", 0.5))
    tie_eps = float(params.get("tie_epsilon", 0.001))
    overlap_margin = float(params.get("overlap_margin_ppm", tol_h * 0.5))
    weights = dict(DEFAULT_WEIGHTS)
    weights.update(params.get("weights") or {})

    atoms = skeleton["atoms"]
    bonds = skeleton["bonds"]
    targets = _build_targets(atoms)
    target_by_id = {t["target_id"]: t for t in targets}
    atom_to_targets: dict[str, list[str]] = {}
    for t in targets:
        for a in t["atoms"]:
            atom_to_targets.setdefault(a, []).append(t["target_id"])

    ok_edges = [e for e in edges if e["type"]["ok"]]

    def edges_from(peak_id: str, etype: str) -> list[dict[str, Any]]:
        return [e for e in ok_edges
                if e["type"]["value"] == etype and (e["from"] == peak_id or e["to"] == peak_id)]

    def edge_partner(e: dict[str, Any], peak_id: str) -> str:
        return e["to"] if e["from"] == peak_id else e["from"]

    # ---- 13C 目标 ----
    carbon_peak_windows: dict[str, list[str]] = {}  # target_id -> peak_ids in window
    for t in targets:
        if t["kind"] != "13C" or t["expected_ppm"] is None:
            continue
        cands = []
        for p in c13_peaks:
            shift = p["shift"]["value"]
            if not p["shift"]["ok"]:
                continue
            if _in_window(shift, t["expected_ppm"], tol_c):
                part = max(0.0, 1.0 - abs(shift - t["expected_ppm"]) / tol_c)
                cands.append({
                    "candidate_id": f"cd-{t['target_id']}-c13:{p['peak_id']}",
                    "peak_ref": f"c13:{p['peak_id']}",
                    "peak_id": p["peak_id"],
                    "score": round(weights["shift"] * part, 6),
                    "components": {"shift": round(part, 6)},
                    "notes": [],
                    "flags": [],
                })
        cands.sort(key=lambda c: (-c["score"], c["peak_id"]))
        t["candidates"] = cands
        carbon_peak_windows[t["target_id"]] = [c["peak_id"] for c in cands]

    # ---- 1H 目标 ----
    proton_targets = [t for t in targets if t["kind"] == "1H"]
    for t in proton_targets:
        if t["expected_ppm"] is None:
            t["candidates"] = []
            continue
        cands = []
        for p in h1_peaks:
            if not p["shift"]["ok"]:
                continue
            shift = p["shift"]["value"]
            if not _in_window(shift, t["expected_ppm"], tol_h):
                continue
            notes: list[str] = []
            shift_part = max(0.0, 1.0 - abs(shift - t["expected_ppm"]) / tol_h)

            integ = p["integration"]["value"]
            if integ is None or t["h_count"] <= 0:
                integ_part = 0.5
                notes.append("积分未知：按 0.5 计")
            else:
                integ_part = max(0.0, 1.0 - abs(integ - t["h_count"]) / max(t["h_count"], 1))

            exp_mult = t["multiplicity_expected"]
            if exp_mult in (None, "none", "."):
                mult_part = 0.5
            elif p["multiplicity"]["value"] == exp_mult:
                mult_part = 1.0
            else:
                mult_part = 0.0
                notes.append(f"多重性不符：期望 {exp_mult}，实测 {p['multiplicity']['value']}")

            # HSQC 证据（仅加权，不作硬约束）
            hsqc_part = 0.0
            if t["attached_carbon"] and t["attached_carbon"] in target_by_id:
                ct = target_by_id[t["attached_carbon"]]
                hits = [e for e in edges_from(p["peak_id"], "HSQC")
                        if edge_partner(e, p["peak_id"]) in carbon_peak_windows.get(ct["target_id"], [])]
                if hits:
                    hsqc_part = 1.0
                else:
                    notes.append("缺失相关边：期望的 HSQC 边不存在（耦合网络仅作证据，不排除候选）")

            # COSY 证据：期望与相邻 1H 目标有边
            neighbour_targets = set()
            for a in t["atoms"]:
                for b in bonds:
                    other = None
                    if b["a"] == a:
                        other = b["b"]
                    elif b["b"] == a:
                        other = b["a"]
                    if other:
                        for tid in atom_to_targets.get(other, []):
                            if tid != t["target_id"] and tid in target_by_id \
                                    and target_by_id[tid]["kind"] == "1H":
                                neighbour_targets.add(tid)
            cosy_hits = 0
            for e in edges_from(p["peak_id"], "COSY"):
                partner = edge_partner(e, p["peak_id"])
                for tid in neighbour_targets:
                    tt = target_by_id[tid]
                    if tt["expected_ppm"] is not None and _in_window(
                            _peak_shift(h1_peaks, partner), tt["expected_ppm"], tol_h):
                        cosy_hits += 1
                        break
            expected_cosy = len(neighbour_targets)
            cosy_part = (cosy_hits / expected_cosy) if expected_cosy else 0.0
            if expected_cosy and cosy_hits < expected_cosy:
                notes.append(f"缺失相关边：期望 {expected_cosy} 条 COSY 边，仅见 {cosy_hits} 条（仅作证据）")

            # HMBC 证据：连到 3 键以内的 13C 目标窗口
            hmbc_hits = 0
            for e in edges_from(p["peak_id"], "HMBC"):
                partner = edge_partner(e, p["peak_id"])
                for ct in targets:
                    if ct["kind"] != "13C":
                        continue
                    if partner not in carbon_peak_windows.get(ct["target_id"], []):
                        continue
                    if t["attached_carbon"] and t["attached_carbon"] in target_by_id:
                        d = _bond_distance(
                            bonds, target_by_id[t["attached_carbon"]]["atoms"][0],
                            ct["atoms"][0])
                        if d is not None and d <= 3:
                            hmbc_hits += 1
            hmbc_part = min(1.0, 0.5 * hmbc_hits)

            score = (weights["shift"] * shift_part
                     + weights["integration"] * integ_part
                     + weights["multiplicity"] * mult_part
                     + weights["hsqc"] * hsqc_part
                     + weights["cosy"] * cosy_part
                     + weights["hmbc"] * hmbc_part)
            cands.append({
                "candidate_id": f"cd-{t['target_id']}-h1:{p['peak_id']}",
                "peak_ref": f"h1:{p['peak_id']}",
                "peak_id": p["peak_id"],
                "score": round(score, 6),
                "components": {
                    "shift": round(shift_part, 6),
                    "integration": round(integ_part, 6),
                    "multiplicity": round(mult_part, 6),
                    "hsqc": round(hsqc_part, 6),
                    "cosy": round(cosy_part, 6),
                    "hmbc": round(hmbc_part, 6),
                },
                "notes": notes,
                "flags": [],
            })
        cands.sort(key=lambda c: (-c["score"], c["peak_id"]))
        t["candidates"] = cands

    # ---- 状态判定 ----
    peak_to_targets: dict[str, list[str]] = {}
    for t in targets:
        for c in t.get("candidates", []):
            peak_to_targets.setdefault(c["peak_ref"], []).append(t["target_id"])

    for t in targets:
        cands = t.get("candidates", [])
        notes: list[str] = []
        if not cands:
            status = "missing"
            notes.append("窗口内无可用峰：不强制唯一匹配，保留空候选集等待补数据")
        else:
            top = cands[0]["score"]
            top_group = [c for c in cands if abs(c["score"] - top) <= tie_eps]
            overlapped = False
            if len(cands) >= 2:
                shifts = []
                for c in cands:
                    src = h1_peaks if t["kind"] == "1H" else c13_peaks
                    p = next((p for p in src if p["peak_id"] == c["peak_id"]), None)
                    if p and p["shift"]["ok"]:
                        shifts.append((c["peak_id"], p["shift"]["value"]))
                for i in range(len(shifts)):
                    for j in range(i + 1, len(shifts)):
                        if abs(shifts[i][1] - shifts[j][1]) < overlap_margin:
                            overlapped = True
            shared = any(len(peak_to_targets[c["peak_ref"]]) > 1 for c in cands)
            if overlapped:
                status = "ambiguous_overlap"
                notes.append("峰形重叠：不强制唯一匹配，保留全部合法候选")
            elif len(top_group) > 1:
                status = "tied"
                notes.append("并列最优：保留全部等分候选，由人工裁定")
            elif shared:
                status = "ambiguous_overlap"
                notes.append("候选峰被多个目标共享：不强制唯一匹配")
            else:
                status = "unique"
        for c in cands:
            if len(peak_to_targets[c["peak_ref"]]) > 1:
                c["flags"].append("shared_peak")
        t["status"] = status
        t["notes"] = notes
        if t["symmetry_group"]:
            t["notes"].append(
                f"对称原子组 {t['symmetry_group']}：原子 {t['atoms']} 化学等价，归属可互换")

    # ---- 未匹配 / 未知状态峰 ----
    unmatched: list[dict[str, Any]] = []
    for key, src in (("h1", h1_peaks), ("c13", c13_peaks)):
        for p in src:
            ref = f"{key}:{p['peak_id']}"
            if not p["shift"]["ok"]:
                unmatched.append({
                    "peak_ref": ref, "reason": "unconvertible_input",
                    "detail": "化学位移无法转换，保留原始表示，不参与候选",
                    "raw": p["shift"]["raw"],
                })
            elif ref not in peak_to_targets:
                unmatched.append({
                    "peak_ref": ref, "reason": "out_of_window",
                    "detail": "不在任何目标容差窗口内",
                    "raw": p["shift"]["raw"],
                })

    return {
        "algorithm": ALGORITHM_VERSION,
        "targets": [
            {
                "target_id": t["target_id"],
                "kind": t["kind"],
                "atoms": t["atoms"],
                "labels": t["labels"],
                "symmetry_group": t["symmetry_group"],
                "expected": t["expected"],
                "h_count": t["h_count"],
                "status": t["status"],
                "notes": t["notes"],
                "candidates": t.get("candidates", []),
            }
            for t in targets
        ],
        "unmatched_peaks": unmatched,
    }


def _peak_shift(peaks: list[dict[str, Any]], peak_id: str) -> float | None:
    for p in peaks:
        if p["peak_id"] == peak_id:
            return p["shift"]["value"]
    return None
