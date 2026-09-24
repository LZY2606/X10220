"""工作台服务层：把事件存储与领域计算串成可操作的事务。

- 原始输入不可覆盖：修订 = 新版本 + 父版本关系。
- 派生值携带算法版本、参数指纹与直接来源。
- 人工确认/否定生成新分支，并返回传播影响（哪些对象过期/被取代）。
- 冲突解决与撤销都写入历史。
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from .domain import STREAMS, candidate_id, effective_assignments, generate_candidates, param_fingerprint, project
from .store import EventStore


class Workbench:
    def __init__(self, store: EventStore):
        self.store = store

    # ---------- 状态 ----------
    def state(self, event_id: Optional[str] = None) -> dict:
        if event_id is None:
            head = self.store.get_head(self.store.current_branch())
            events = self.store.ancestors(head) if head else []
        else:
            events = self.store.ancestors(event_id)
        st = project(events)
        st["branches"] = self.store.branches()
        st["current_branch"] = self.store.current_branch()
        st["head_event"] = event_id or self.store.get_head(self.store.current_branch())
        st["assignments"] = effective_assignments(st)
        return st

    def _head(self) -> Optional[str]:
        return self.store.get_head(self.store.current_branch())

    def _append(self, kind: str, payload: dict, actor: str, branch: Optional[str] = None,
                algo_version: Optional[str] = None, param_fp: Optional[str] = None) -> dict:
        branch = branch or self.store.current_branch()
        head = self.store.get_head(branch)
        parents = [head] if head else []
        event = self.store.append(kind, payload, actor, branch, parents, algo_version, param_fp)
        self.store.set_head(branch, event["event_id"])
        return event

    # ---------- 项目与输入 ----------
    def create_project(self, name: str, case_id: str, actor: str) -> dict:
        return self._append("project_created", {"name": name, "case_id": case_id}, actor)

    def _next_version(self, stream: str) -> str:
        st = self.state()
        n = len(st["input_history"].get(stream, [])) + 1
        return f"{stream}-v{n}"

    def commit_input(self, stream: str, data: dict, source_file: dict, actor: str) -> dict:
        if stream not in STREAMS:
            raise ValueError(f"未知输入流 {stream!r}")
        st = self.state()
        if stream in st["inputs"]:
            raise ValueError(f"输入流 {stream} 已提交，原始输入不可覆盖，请使用修订")
        payload = {"stream": stream, "version_id": self._next_version(stream),
                   "parent_version_id": None, "data": data, "source_file": source_file}
        return self._append("input_committed", payload, actor)

    def revise_input(self, stream: str, data: dict, source_file: Optional[dict], actor: str, note: str = "") -> dict:
        st = self.state()
        if stream not in st["inputs"]:
            raise ValueError(f"输入流 {stream} 尚未提交，无法修订")
        parent = st["inputs"][stream]
        payload = {"stream": stream, "version_id": self._next_version(stream),
                   "parent_version_id": parent["version_id"], "data": data,
                   "source_file": source_file or parent["source_file"], "note": note}
        before = self._status_snapshot()
        event = self._append("input_revised", payload, actor)
        return {"event": event, "impact": self._impact(before)}

    def commit_params(self, params: dict, actor: str) -> dict:
        fp = param_fingerprint(params)
        return self._append("params_committed", {"params": params, "changed_keys": sorted(params)}, actor,
                            param_fp=fp)

    def revise_params(self, patch: dict, changed_keys: list[str], actor: str, note: str = "") -> dict:
        st = self.state()
        if st["params"] is None:
            raise ValueError("参数尚未提交")
        params = {**st["params"], **patch}
        before = self._status_snapshot()
        event = self._append("params_revised", {"params": params, "changed_keys": changed_keys, "note": note},
                             actor, param_fp=param_fingerprint(params))
        return {"event": event, "impact": self._impact(before)}

    # ---------- 候选生成 ----------
    def regenerate(self, actor: str) -> dict:
        st = self.state()
        missing = [s for s in STREAMS if s not in st["inputs"]]
        if missing:
            raise ValueError(f"缺少输入流: {missing}")
        if st["params"] is None:
            raise ValueError("参数尚未提交")
        inputs = {s: {"version_id": st["inputs"][s]["version_id"], "data": st["inputs"][s]["data"]} for s in STREAMS}
        result = generate_candidates(inputs, st["params"])
        head = self._head()
        set_seq = len(st["candidate_sets"]) + 1
        payload = {"set_id": f"cset-{set_seq:03d}", **result}
        before = self._status_snapshot()
        event = self._append("candidates_generated", payload, actor,
                             algo_version=result["algo_version"], param_fp=result["param_fingerprint"])
        return {"event": event, "set_id": payload["set_id"], "impact": self._impact(before),
                "candidates": result["candidates"], "diagnostics": result["diagnostics"]}

    # ---------- 人工决定（生成新分支） ----------
    def decide(self, candidate: str, verdict: str, editor: str, rationale: str = "",
               from_event: Optional[str] = None) -> dict:
        if verdict not in ("confirm", "reject"):
            raise ValueError("verdict 必须是 confirm 或 reject")
        fork = from_event or self._head()
        parent_branch = self.store.current_branch()
        branch = self.store.create_branch(parent_branch, fork)
        decision_id = "dec-" + hashlib.sha256(
            json.dumps({"c": candidate, "v": verdict, "e": editor, "f": fork}, sort_keys=True).encode()
        ).hexdigest()[:8]
        payload = {"decision_id": decision_id, "candidate_id": candidate, "verdict": verdict,
                   "editor": editor, "rationale": rationale, "fork_event": fork}
        before = self._status_snapshot()
        event = self._append("decision_made", payload, editor, branch=branch)
        self.store.set_current_branch(branch)
        conflict = self._detect_conflict(decision_id, candidate, verdict, event)
        return {"event": event, "branch": branch, "decision_id": decision_id,
                "conflict": conflict, "impact": self._impact(before)}

    def _detect_conflict(self, decision_id: str, candidate: str, verdict: str, event: dict) -> Optional[dict]:
        ancestor_ids = {e["event_id"] for e in self.store.ancestors(event["event_id"])}
        opposing = []
        for e in self.store.events():
            if e["kind"] != "decision_made":
                continue
            p = e["payload"]
            if p["candidate_id"] == candidate and p["verdict"] != verdict and e["event_id"] not in ancestor_ids:
                opposing.append({"decision_id": p["decision_id"], "event_id": e["event_id"],
                                 "verdict": p["verdict"], "editor": p["editor"]})
        if not opposing:
            return None
        conflict_id = "conf-" + hashlib.sha256(
            json.dumps(sorted([decision_id] + [o["decision_id"] for o in opposing])).encode()
        ).hexdigest()[:8]
        payload = {"conflict_id": conflict_id, "candidate_id": candidate,
                   "sides": [{"decision_id": decision_id, "verdict": verdict, "event_id": event["event_id"]}] + opposing}
        self._append("conflict_recorded", payload, "system")
        return payload

    def resolve_conflict(self, conflict_id: str, chosen_decision_id: str, actor: str, rationale: str = "") -> dict:
        before = self._status_snapshot()
        event = self._append("conflict_resolved", {"conflict_id": conflict_id,
                                                   "chosen_decision_id": chosen_decision_id,
                                                   "rationale": rationale}, actor)
        return {"event": event, "impact": self._impact(before)}

    def revert_decision(self, decision_id: str, actor: str, rationale: str = "") -> dict:
        before = self._status_snapshot()
        event = self._append("decision_reverted", {"decision_id": decision_id, "rationale": rationale}, actor)
        return {"event": event, "impact": self._impact(before)}

    def record_judgment(self, judgment: dict, actor: str) -> dict:
        return self._append("judgment_recorded", judgment, actor)

    # ---------- 影响（传播） ----------
    def _status_snapshot(self) -> dict:
        st = self.state()
        out = {}
        for cset in st["candidate_sets"]:
            for c in cset["candidates"]:
                out[c["candidate_id"]] = c["status"]
        return out

    def _impact(self, before: dict) -> dict:
        after = self._status_snapshot()
        newly_stale = sorted(cid for cid, s in after.items() if s == "stale" and before.get(cid) == "current")
        newly_superseded = sorted(cid for cid, s in after.items() if s == "superseded" and before.get(cid) != "superseded")
        return {"newly_stale": newly_stale, "newly_superseded": newly_superseded,
                "counts": {"stale": sum(1 for s in after.values() if s == "stale"),
                           "current": sum(1 for s in after.values() if s == "current"),
                           "superseded": sum(1 for s in after.values() if s == "superseded")}}
