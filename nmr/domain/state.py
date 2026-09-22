"""领域状态：项目不可变事件序列在内存中的折叠（fold）结果。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

ALGORITHM_VERSION = "assign-1.0.0"

DEFAULT_PARAMS: Dict[str, Any] = {
    "tolerance_1h_ppm": 0.05,
    "tolerance_13c_ppm": 0.20,
    "global_bias_1h_ppm": 0.0,
    "global_bias_13c_ppm": 0.0,
    "overlap_1h_ppm": 0.01,
    "overlap_13c_ppm": 0.05,
    "score_weights": {
        "shift_error": 1.0,
        "hsqc": 0.30,
        "cosy": 0.20,
        "hmbc": 0.20,
        "missing_edge_penalty": 0.05,
    },
}

MAIN_BRANCH = "main"


def new_state(project_id: str, name: str) -> Dict[str, Any]:
    return {
        "project_id": project_id,
        "name": name,
        "sources": {},          # kind -> source record dict (含 version/body/digest)
        "source_versions": defaultdict(dict),  # kind -> {version: source record}
        "params": {"version": 0, "params": DEFAULT_PARAMS, "parent_version": None,
                   "reason": "initial"},
        "snapshots": [],        # 每次候选计算事件（永不删除）
        "decisions": [],        # 决定事件（含撤销标记、分支）
        "conflicts": [],
        "checkouts": {},        # branch -> event_id
        "atoms_latest": None,   # 当前骨架源 record
    }


def active_source_versions(state: Dict[str, Any]) -> Dict[str, int]:
    return {kind: max(recs.keys()) for kind, recs in state["source_versions"].items()}


def latest_sources(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {kind: recs[max(recs.keys())]
            for kind, recs in state["source_versions"].items() if recs}


def skeleton_version(state: Dict[str, Any]) -> Optional[int]:
    recs = state["source_versions"].get("skeleton")
    return max(recs.keys()) if recs else None


def build_skeleton_graph(atoms: List[Dict[str, Any]],
                         bonds: List[Dict[str, Any]]) -> Dict[str, Any]:
    labels = {a["label"] for a in atoms}
    adj: Dict[str, Set[str]] = {l: set() for l in labels}
    for b in bonds:
        adj.setdefault(b["from"], set()).add(b["to"])
        adj.setdefault(b["to"], set()).add(b["from"])
    return {"labels": labels, "adj": adj}


def graph_distance(graph: Dict[str, Any], a: str, b: str, max_dist: int = 5) -> Optional[int]:
    if a not in graph["labels"] or b not in graph["labels"]:
        return None
    if a == b:
        return 0
    seen = {a}
    frontier = [(a, 0)]
    while frontier:
        node, dist = frontier.pop(0)
        if dist >= max_dist:
            continue
        for nxt in graph["adj"].get(node, ()):
            if nxt in seen:
                continue
            if nxt == b:
                return dist + 1
            seen.add(nxt)
            frontier.append((nxt, dist + 1))
    return None


def equivalence_classes(atoms: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """对称原子等价类：symmetry_group 相同且非平凡（成员>1）即互为等价原子。"""
    groups: Dict[str, List[str]] = defaultdict(list)
    for a in atoms:
        groups[a["symmetry_group"]].append(a["label"])
    classes: Dict[str, Set[str]] = {}
    for labels in groups.values():
        if len(labels) > 1:
            members = set(labels)
            for label in labels:
                classes[label] = members
    return classes


def tolerance_for(nucleus: str, params: Dict[str, Any]) -> float:
    return params["tolerance_1h_ppm"] if nucleus == "1H" else params["tolerance_13c_ppm"]


def bias_for(nucleus: str, params: Dict[str, Any]) -> float:
    return params.get("global_bias_1h_ppm" if nucleus == "1H"
                      else "global_bias_13c_ppm", 0.0)


def overlap_threshold(nucleus: str, params: Dict[str, Any]) -> float:
    return params.get("overlap_1h_ppm" if nucleus == "1H" else "overlap_13c_ppm", 0.0)
