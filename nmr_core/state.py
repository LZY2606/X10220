"""状态折叠：把事件流折叠成某一时刻的复核状态。

- 过期（stale）结果仍可查看，但明确标注原因，不能冒充当前结果。
- 局部输入变化只使依赖它的候选集过期（depends_on 精确到源版本）。
- 回到任意旧事件（as_of）即恢复当时的依赖版本与可见结论。
"""
from __future__ import annotations

from typing import Any


def fold(events: list[dict[str, Any]]) -> dict[str, Any]:
    st: dict[str, Any] = {
        "project": None,
        "sources": {},
        "params": {"versions": [], "current": None},
        "ingests": {},
        "candidate_sets": [],
        "decisions": [],
        "conflicts": [],
        "branches": [],
        "diagnostics": [],
        "head_seq": events[-1]["seq"] if events else 0,
        "head_event": events[-1]["event_id"] if events else None,
    }

    for e in events:
        kind = e["kind"]
        p = e["payload"]
        if kind == "project_created":
            st["project"] = {
                "project_id": p["project_id"], "name": p["name"],
                "narrative": p.get("narrative", ""), "created_by": e["actor"],
                "created_at": e["created_at"],
            }
        elif kind == "source_committed":
            key = p["source_key"]
            entry = st["sources"].setdefault(key, {"versions": [], "current": None, "by_version": {}})
            entry["versions"].append(p["version_id"])
            entry["current"] = p["version_id"]
            entry["by_version"][p["version_id"]] = {
                "version_id": p["version_id"],
                "parent_version_id": p.get("parent_version_id"),
                "kind": p["kind"],
                "sha256": p["sha256"],
                "note": p.get("note", ""),
                "committed_by": e["actor"],
                "committed_at": e["created_at"],
                "event_id": e["event_id"],
                "raw_text": p["raw_text"],
                "meta": p.get("meta", {}),
            }
        elif kind == "ingest_completed":
            st["ingests"][p["version_id"]] = p["parsed"]
            for d in p["parsed"].get("diagnostics", []):
                st["diagnostics"].append({**d, "source_key": p["source_key"],
                                          "version_id": p["version_id"]})
        elif kind == "params_committed":
            st["params"]["versions"].append({
                "version_id": p["version_id"],
                "parent_version_id": p.get("parent_version_id"),
                "params": p["params"],
                "fingerprint": p["fingerprint"],
                "note": p.get("note", ""),
                "committed_by": e["actor"],
                "committed_at": e["created_at"],
                "event_id": e["event_id"],
            })
            st["params"]["current"] = p["version_id"]
        elif kind == "candidates_computed":
            st["candidate_sets"].append({
                "set_id": p["set_id"],
                "algorithm_version": p["algorithm_version"],
                "param_fingerprint": p["param_fingerprint"],
                "params_version": p["params_version"],
                "depends_on": p["depends_on"],
                "result": p["result"],
                "computed_by": e["actor"],
                "computed_at": e["created_at"],
                "event_id": e["event_id"],
            })
        elif kind == "branch_created":
            st["branches"].append({
                "branch": p["branch"], "from_event": p["from_event"],
                "reason": p.get("reason", ""), "created_by": e["actor"],
                "created_at": e["created_at"],
            })
        elif kind == "decision_made":
            st["decisions"].append({
                "decision_id": p["decision_id"],
                "target_candidate_id": p["target_candidate_id"],
                "target_id": p.get("target_id"),
                "target_set_id": p["target_set_id"],
                "verdict": p["verdict"],
                "rationale": p.get("rationale", ""),
                "judgment_basis": p.get("judgment_basis", []),
                "actor": e["actor"],
                "branch": e["branch"],
                "event_id": e["event_id"],
                "created_at": e["created_at"],
                "reverted": False,
                "overruled": False,
                "contested": False,
            })
        elif kind == "decision_reverted":
            for d in st["decisions"]:
                if d["decision_id"] == p["reverts"]:
                    d["reverted"] = True
                    d["revert_reason"] = p.get("reason", "")
                    d["reverted_by"] = e["actor"]
        elif kind == "conflict_recorded":
            st["conflicts"].append({
                "conflict_id": p["conflict_id"],
                "decision_ids": p["decision_ids"],
                "target_candidate_id": p["target_candidate_id"],
                "actors": p["actors"],
                "status": "open",
                "recorded_at": e["created_at"],
                "event_id": e["event_id"],
            })
        elif kind == "conflict_resolved":
            for c in st["conflicts"]:
                if c["conflict_id"] == p["conflict_id"]:
                    c["status"] = "resolved"
                    c["chosen_decision_id"] = p["chosen_decision_id"]
                    c["resolution_rationale"] = p.get("rationale", "")
                    c["resolved_by"] = e["actor"]
                    c["resolved_at"] = e["created_at"]
                    for did in c["decision_ids"]:
                        if did != p["chosen_decision_id"]:
                            for d in st["decisions"]:
                                if d["decision_id"] == did:
                                    d["overruled"] = True

    # 冲突未决 -> 相关决定标记 contested
    open_conflict_dids = set()
    for c in st["conflicts"]:
        if c["status"] == "open":
            open_conflict_dids.update(c["decision_ids"])
    for d in st["decisions"]:
        if d["decision_id"] in open_conflict_dids:
            d["contested"] = True
        d["effective"] = not d["reverted"] and not d["overruled"]

    # 候选集过期判定：只使依赖变化的集合过期，旧集合仍可查看
    current_params_fp = None
    if st["params"]["current"]:
        current_params_fp = st["params"]["versions"][-1]["fingerprint"]
    latest_set_id = st["candidate_sets"][-1]["set_id"] if st["candidate_sets"] else None
    for cs in st["candidate_sets"]:
        reasons: list[dict[str, Any]] = []
        for key, vid in cs["depends_on"].items():
            cur = st["sources"].get(key, {}).get("current")
            if cur and cur != vid:
                reasons.append({
                    "type": "source_superseded", "source_key": key,
                    "used_version": vid, "current_version": cur,
                })
        if current_params_fp and cs["param_fingerprint"] != current_params_fp:
            reasons.append({
                "type": "params_changed",
                "used_fingerprint": cs["param_fingerprint"],
                "current_fingerprint": current_params_fp,
            })
        if cs["set_id"] != latest_set_id and not reasons:
            reasons.append({"type": "superseded_by_newer_set", "current_set": latest_set_id})
        cs["stale"] = bool(reasons)
        cs["stale_reasons"] = reasons

    # 候选上的决定标注
    decisions_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for d in st["decisions"]:
        decisions_by_candidate.setdefault(d["target_candidate_id"], []).append(d)
    for cs in st["candidate_sets"]:
        for t in cs["result"]["targets"]:
            confirmed_id = None
            for c in t["candidates"]:
                ds = decisions_by_candidate.get(c["candidate_id"], [])
                eff = [d for d in ds if d["effective"]]
                c["decisions"] = [
                    {"decision_id": d["decision_id"], "verdict": d["verdict"],
                     "actor": d["actor"], "effective": d["effective"],
                     "reverted": d["reverted"], "overruled": d["overruled"],
                     "contested": d["contested"], "branch": d["branch"]}
                    for d in ds
                ]
                if any(d["verdict"] == "confirm" for d in eff):
                    c["decision_status"] = "confirmed"
                    confirmed_id = c["candidate_id"]
                elif any(d["contested"] for d in ds):
                    c["decision_status"] = "contested"
                elif any(d["verdict"] == "reject" for d in eff):
                    c["decision_status"] = "rejected"
                else:
                    c["decision_status"] = "none"
            if confirmed_id:
                for c in t["candidates"]:
                    if c["candidate_id"] != confirmed_id and c["decision_status"] == "none":
                        c["decision_status"] = "displaced"

    return st
