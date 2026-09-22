"""服务层：把领域操作落成事件。API 与案例包共用同一条代码路径。"""
from __future__ import annotations

from typing import Dict, Optional

from .core import DEFAULT_PARAMS, Store, annotate, fingerprint, fold, stable_id
from .engine import compute_candidates
from .ingest import parse_input


def current_state(store: Store, upto: Optional[int] = None) -> Dict:
    return annotate(fold(store.events(upto)))


def submit_input(store: Store, name: str, fmt: str, raw: str) -> Dict:
    """原始输入一经提交不可覆盖；修订以新版本 + 父版本表达。"""
    state = fold(store.events())
    parsed = parse_input(name, fmt, raw)
    parent = state["inputs"].get(name)
    version = 1 if parent is None else state["input_versions"][parent]["version"] + 1
    input_id = stable_id("input", name, version, fingerprint(raw))
    payload = {"input_id": input_id, "name": name, "format": fmt,
               "version": version, "parent": parent, "raw": raw,
               "parsed": parsed["parsed"], "diagnostics": parsed["diagnostics"]}
    return store.append("input_submitted", payload)


def revise_params(store: Store, updates: Dict) -> Dict:
    state = fold(store.events())
    if state["params"]:
        base = dict(state["params"][-1]["params"])
        parent = state["params"][-1]["params_id"]
    else:
        base = dict(DEFAULT_PARAMS)
        parent = None
    base.update(updates)
    pid = stable_id("params", fingerprint(base))
    payload = {"params_id": pid, "params": base, "parent": parent,
               "fingerprint": fingerprint(base)}
    return store.append("params_revised", payload)


def compute(store: Store) -> Dict:
    state = fold(store.events())
    if not state["params"]:
        revise_params(store, {})  # 首次计算前固化默认参数版本
        state = fold(store.events())
    params_payload = state["params"][-1]
    inputs, deps = {}, {}
    for name, iid in state["inputs"].items():
        iv = state["input_versions"][iid]
        inputs[name] = {"format": iv["format"], "parsed": iv["parsed"]}
        deps[name] = iid
    result = compute_candidates(inputs, params_payload["params"], deps,
                                params_payload["params_id"])
    return store.append("candidates_computed", result)


def decide(store: Store, atom_id: str, candidate_id: str, action: str,
           editor: str, base_branch: str = "main") -> Dict:
    """人工确认/否定：生成新分支，并把传播影响写入事件载荷。"""
    if action not in ("confirm", "reject"):
        raise ValueError("action 必须是 confirm 或 reject")
    state = current_state(store)
    aset = None
    for s in state["candidate_sets"].values():
        if any(c["candidate_id"] == candidate_id for c in s["candidates"]):
            aset = s
    if aset is None:
        raise ValueError("未知候选: %s" % candidate_id)
    cand = next(c for c in aset["candidates"]
                if c["candidate_id"] == candidate_id)
    impact = {"siblings_rejected": [], "peak_conflicts": []}
    if action == "confirm":
        for c in aset["candidates"]:
            if c["atom_id"] == atom_id and c["candidate_id"] != candidate_id:
                impact["siblings_rejected"].append(c["candidate_id"])
        for c in aset["candidates"]:
            if c["peak_id"] == cand["peak_id"] and c["atom_id"] != atom_id:
                impact["peak_conflicts"].append({
                    "peak_id": cand["peak_id"], "atom_id": c["atom_id"],
                    "candidate_id": c["candidate_id"],
                    "note": "重叠/共享峰不被强制唯一匹配，仅提示"})
    seq = store.next_seq()
    payload = {
        "decision_id": stable_id("dec", seq, atom_id, candidate_id, action, editor),
        "set_id": aset["set_id"], "atom_id": atom_id,
        "candidate_id": candidate_id, "action": action, "editor": editor,
        "branch": base_branch, "new_branch": "b%d" % seq,
        "impact": impact,
    }
    return store.append("decision", payload, branch=base_branch)


def undo(store: Store, decision_id: str, editor: str) -> Dict:
    return store.append("undo", {"target_decision_id": decision_id,
                                 "editor": editor})


def resolve_conflict(store: Store, conflict_id: str,
                     winner_decision_id: str, editor: str) -> Dict:
    return store.append("conflict_resolved", {
        "conflict_id": conflict_id,
        "winner_decision_id": winner_decision_id, "editor": editor})
