"""影响传播：局部源改动只标记依赖对象过期，并给出受影响对象集合。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .replay import replay, ViewState, SHIFT_CONTEXT_FIELDS


def impact_of_event(project, branch: str, event_id: str) -> Dict[str, Any]:
    before = replay(project, branch, upto_event_id=_prev_id(project, branch, event_id))
    after = replay(project, branch, upto_event_id=event_id)
    ev = next(e for e in after.events if e["id"] == event_id)
    etype = ev["type"]
    impact: Dict[str, Any] = {"event_id": event_id, "type": etype,
                              "scope": None, "affected": {}}

    if etype == "SourceSubmitted":
        slot = ev["payload"]["slot"]
        changed_sources = {ev["payload"]["source_id"]}
        affected_snapshots = [s["id"] for s in before.snapshots]
        affected_candidates, affected_decisions = _dependent_objects(
            before, changed_sources)
        impact["scope"] = {"kind": "source", "slot": slot, "local": True}
        impact["affected"] = {
            "invalidated_snapshots": affected_snapshots,
            "candidates_by_source": affected_candidates,
            "decisions_now_stale": affected_decisions,
            "unaffected_nuclei_note":
                "仅依赖未改动源版本的核不受影响；过期结果可查看但不冒充当前结果"}
    elif etype == "ParametersSet":
        impact["scope"] = {"kind": "params",
                           "fingerprint": ev["payload"]["fingerprint"]}
        impact["affected"] = {
            "invalidated_snapshots": [s["id"] for s in before.snapshots],
            "note": "参数指纹变化使所有派生快照过期；旧快照仍可查看与重放"}
    elif etype == "ContextChanged":
        fields = sorted(ev["payload"].get("changes", {}).keys())
        shift_fields = [f for f in fields if f in SHIFT_CONTEXT_FIELDS]
        impact["scope"] = {"kind": "context", "changed_fields": fields,
                           "shifts_candidates": bool(shift_fields)}
        if shift_fields:
            impact["affected"] = {
                "invalidated_snapshots": [s["id"] for s in before.snapshots],
                "stale_basis_decisions":
                    [n for n, a in before.assignments.items()],
                "note": "溶剂/温度/参考标准变更只使相关候选过期；旧决定仍可重放"}
        else:
            impact["affected"] = {
                "invalidated_snapshots": [],
                "note": "仪器频率等元数据变更不使归属候选过期"}
    elif etype in ("DecisionMade", "DecisionReverted", "ConflictResolved"):
        nuclei = ev["payload"].get("nuclei", [])
        impact["scope"] = {"kind": "decision", "nuclei": nuclei}
        impact["affected"] = {"changed_nuclei": nuclei,
                              "note": "人工决定生成新分支/事件并可撤销，不删除历史"}
    elif etype == "BranchCreated":
        impact["scope"] = {"kind": "branch", "name": ev["payload"]["name"]}
    return impact


def _prev_id(project, branch: str, event_id: str) -> Optional[str]:
    events = project.events(branch)
    ids = [e["id"] for e in events]
    if event_id not in ids:
        return events[-1]["id"] if events else None
    i = ids.index(event_id)
    return ids[i - 1] if i > 0 else None


def _dependent_objects(state: ViewState, changed_sources: set):
    cands: Dict[str, List[str]] = {}
    decisions: List[str] = []
    for snap in state.snapshots:
        for c in snap.get("candidates", []):
            if set(c.get("direct_sources", [])) & changed_sources:
                cands.setdefault(snap["id"], []).append(c["id"])
    current = next((s for s in reversed(state.snapshots)
                    if s["status"] == "current"), None)
    if current:
        affected_cids = {cid for ids in cands.values() for cid in ids}
        for nuc, a in state.assignments.items():
            if a.get("candidate_id") in affected_cids:
                decisions.append(nuc)
    return cands, sorted(decisions)
