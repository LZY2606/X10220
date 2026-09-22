"""事件溯源编排引擎。

所有写操作都以 append-only 事件表达（原始提交、候选快照、人工裁定、
冲突解决、撤销、检出移动）。读状态时沿事件树从根折叠：
回任意旧事件即恢复当时的参数版本、源版本与可见结论，
过期结果保留在历史快照中但不冒充当前结果。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ..clock import Clock
from ..identifiers import (atom_id, candidate_id, condition_id, content_hash,
                           decision_id, edge_id, event_id, paramset_id,
                           peak_id, source_id, unknown_id)
from ..rng import seeded_rng
from ..storage import Storage
from . import scoring
from .state import ALGORITHM_VERSION, DEFAULT_PARAMS, MAIN_BRANCH, new_state


class EngineError(ValueError):
    pass


class Engine:
    def __init__(self, storage: Storage, clock: Optional[Clock] = None,
                 seed: int = 20260922) -> None:
        self.storage = storage
        self.clock = clock or Clock()
        self.rng = seeded_rng(seed)

    # ------------------------------------------------------------------
    # 事件写入
    # ------------------------------------------------------------------
    def _next_seq(self, project_id: str) -> int:
        return self.storage.count_events(project_id) + 1

    def _append(self, project_id: str, kind: str, payload: Dict[str, Any],
                parent_id: Optional[str], actor: str, branch_id: str = MAIN_BRANCH,
                undo_of: Optional[str] = None) -> Dict[str, Any]:
        seq = self._next_seq(project_id)
        fingerprint = content_hash(payload)
        eid = event_id(project_id, seq, fingerprint)
        event = {
            "event_id": eid, "project_id": project_id, "seq": seq,
            "branch_id": branch_id, "kind": kind, "parent_id": parent_id,
            "actor": actor, "clock": self.clock.now(),
            "fingerprint": fingerprint, "payload": payload, "undo_of": undo_of,
        }
        self.storage.append_event(event)
        self.storage.set_head(project_id, branch_id, eid)
        return event

    def create_project(self, project_id: str, name: str,
                       actor: str = "alice") -> Dict[str, Any]:
        if self.storage.count_events(project_id):
            raise EngineError(f"项目已存在: {project_id}")
        payload = {"project_id": project_id, "name": name,
                   "algorithm_version": ALGORITHM_VERSION}
        event = self._append(project_id, "project_created", payload, None, actor)
        self.storage.set_head(project_id, MAIN_BRANCH, event["event_id"])
        return event

    def submit_source(self, project_id: str, parsed: Dict[str, Any],
                      actor: str = "alice") -> Dict[str, Any]:
        root = self.require_root(project_id)
        current = self.fold(project_id)
        kind = parsed["kind"]
        existing = current["source_versions"].get(kind, {})
        version = parsed["file_version"]
        if version < 1:
            raise EngineError("file_version 必须 >= 1")
        if version in existing:
            raise EngineError(f"{kind} 版本 {version} 已存在；原始输入不可覆盖，请提交新版本")

        record = self._normalize_source(project_id, parsed)
        prior = max(existing.keys()) if existing else None
        parent_src = existing[prior]["stable_id"] if prior is not None else None
        record["version"] = version
        record["parent_source_id"] = parent_src

        diag_payload = [
            {"severity": d.severity if hasattr(d, "severity") else d.get("severity"),
             "code": d.get("code"), "message": d.get("message"),
             "field": d.get("field"), "file_index": d.get("file_index"),
             "raw": d.get("raw")}
            for d in parsed.get("diagnostics", [])
        ]
        payload = {
            "source": {k: v for k, v in record.items() if k != "introduced_event"},
            "diagnostics": diag_payload,
        }
        event = self._append(project_id, "source_submitted", payload,
                             self.head_of(project_id), actor)
        record["introduced_event"] = event["event_id"]
        self.storage.insert_source(record)

        # 依赖满足即自动产生候选快照（局部变化只重算相关候选；快照保留全部结果）
        auto = self._maybe_compute(project_id, actor, trigger_event=event["event_id"])
        return {"event": event, "auto_compute": auto}

    def revise_parameters(self, project_id: str, new_params: Dict[str, Any],
                          reason: str, actor: str = "alice") -> Dict[str, Any]:
        self.require_root(project_id)
        current = self.fold(project_id)
        merged = {**current["params"]["params"], **new_params}
        version = current["params"]["version"] + 1
        payload = {"version": version, "params": merged,
                   "parent_version": current["params"]["version"],
                   "reason": reason,
                   "fingerprint": content_hash(merged)}
        event = self._append(project_id, "params_revised", payload,
                             self.head_of(project_id), actor)
        auto = self._maybe_compute(project_id, actor, trigger_event=event["event_id"])
        return {"event": event, "auto_compute": auto}

    # ------------------------------------------------------------------
    # 规范化（原始保留，派生层才产生 stable_id / value）
    # ------------------------------------------------------------------
    def _normalize_source(self, project_id: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
        kind = parsed["kind"]
        version = parsed["file_version"]
        sid = source_id(project_id, kind, version, parsed["filename"])
        record: Dict[str, Any] = {
            "stable_id": sid, "project_id": project_id, "kind": kind,
            "filename": parsed["filename"], "file_version": version,
            "raw_text": parsed["raw_text"], "digest": content_hash(parsed["raw_doc"]),
        }
        body = parsed["body"]
        if "peaks" in body:
            record["peaks"] = [
                {**p, "stable_id": peak_id(project_id, sid, p["file_index"], p["key"])}
                for p in body["peaks"]]
        if "edges" in body:
            record["edges"] = [
                {**e, "stable_id": edge_id(project_id, sid, e["file_index"], e["key"])}
                for e in body["edges"]]
        if "atoms" in body:
            record["atoms"] = [
                {**a, "stable_id": atom_id(project_id, a["label"])}
                for a in body["atoms"]]
            record["bonds"] = body.get("bonds", [])
            record["expected_coupling"] = body.get(
                "expected_coupling", {"COSY": [], "HSQC": [], "HMBC": []})
        if "meta" in body:
            record["meta"] = body["meta"]
        return record

    def _current_material(self, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sources = self._latest_records(state)
        if "skeleton" not in sources:
            return None
        peaks: List[Dict[str, Any]] = []
        for k in ("1h_peaks", "13c_peaks"):
            if k in sources:
                for p in sources[k].get("peaks", []):
                    peaks.append(self._peak_view(p, sources[k]))
        edges: List[Dict[str, Any]] = []
        for k in ("cosy", "hsqc", "hmbc"):
            if k in sources:
                for e in sources[k].get("edges", []):
                    edges.append(self._edge_view(e))
        skel = sources["skeleton"]
        h_atoms = [self._atom_view(a) for a in skel.get("atoms", [])
                   if a["nucleus"] == "1H"]
        c_atoms = [self._atom_view(a) for a in skel.get("atoms", [])
                   if a["nucleus"] == "13C"]
        meta = sources.get("experiment_meta", {}).get("meta")
        return {"peaks": peaks, "edges": edges, "h_atoms": h_atoms,
                "c_atoms": c_atoms, "bonds": skel.get("bonds", []),
                "curated_edges": skel.get(
                    "expected_coupling", {"COSY": [], "HSQC": [], "HMBC": []}),
                "meta": meta}

    @staticmethod
    def _latest_records(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {kind: recs[max(recs.keys())]
                for kind, recs in state["source_versions"].items() if recs}

    @staticmethod
    def _peak_view(p: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "key": p["key"], "nucleus": p["nucleus"],
            "shift_value": p["shift"]["value"], "shift_raw": p["shift"]["raw"],
            "shift_unit": p["shift"]["unit"], "shift_basis": p["shift"]["basis"],
            "intensity_value": (p.get("intensity") or {}).get("value"),
            "intensity_raw": (p.get("intensity") or {}).get("raw"),
            "intensity_unit": (p.get("intensity") or {}).get("unit"),
            "integration_value": (p.get("integration") or {}).get("value"),
            "integration_raw": (p.get("integration") or {}).get("raw"),
            "integration_unit": (p.get("integration") or {}).get("unit"),
            "multiplicity_value": (p.get("multiplicity") or {}).get("value"),
            "multiplicity_raw": (p.get("multiplicity") or {}).get("raw"),
            "source_id": source["stable_id"],
            "source_version": source["file_version"],
            "source_kind": source["kind"],
            "filename": source["filename"],
            "stable_id": p["stable_id"], "file_index": p["file_index"],
        }

    @staticmethod
    def _edge_view(e: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "key": e["key"], "experiment": e["experiment"],
            "from_ref": e["from_ref"], "to_ref": e["to_ref"],
            "stable_id": e["stable_id"], "file_index": e["file_index"],
            "from_axis_value": (e.get("from_axis") or {}).get("value"),
            "to_axis_value": (e.get("to_axis") or {}).get("value"),
        }

    @staticmethod
    def _atom_view(a: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "label": a["label"], "element": a["element"], "nucleus": a["nucleus"],
            "symmetry_group": a["symmetry_group"],
            "equivalent_to": a.get("equivalent_to"),
            "attached_to": a.get("attached_to"), "protons": a.get("protons"),
            "predicted_value": a["predicted_shift"]["value"],
            "predicted_raw": a["predicted_shift"]["raw"],
        }

    def _maybe_compute(self, project_id: str, actor: str,
                       trigger_event: str) -> Optional[Dict[str, Any]]:
        state = self.fold(project_id)
        material = self._current_material(state)
        if material is None or not material["peaks"]:
            return None
        if not material["h_atoms"] and not material["c_atoms"]:
            return None
        meta = material.get("meta")
        meta_hash = content_hash(meta) if meta else "no_meta"
        skeleton_ver = state["source_versions"]["skeleton"] and \
            max(state["source_versions"]["skeleton"].keys())
        param_ver = state["params"]["version"]
        cid = condition_id(project_id, skeleton_ver, param_ver, meta_hash)
        if state["snapshots"] and state["snapshots"][-1]["condition_id"] == cid:
            # 变更不影响本条件（例如重复提交无关源）时不产生重复快照
            return None

        # 确定哪些直接来源版本相对上一快照发生变化（局部失效范围）
        prev_versions = state["snapshots"][-1]["source_versions"] if state["snapshots"] else {}
        changed_sources = []
        for kind, ver in self._active_versions(state).items():
            if prev_versions.get(kind) != ver:
                changed_sources.append({"kind": kind, "from_version": prev_versions.get(kind),
                                        "to_version": ver})
        params_changed = bool(state["snapshots"]) and \
            state["snapshots"][-1]["param_version"] != param_ver
        meta_changed = bool(state["snapshots"]) and \
            state["snapshots"][-1]["meta_hash"] != meta_hash

        result = scoring.compute_candidates(
            peaks=material["peaks"], h_atoms=material["h_atoms"],
            c_atoms=material["c_atoms"], bonds=material["bonds"],
            edges=material["edges"], params=state["params"]["params"],
            rng=self.rng, curated_edges=material.get("curated_edges"))
        candidates = self._stamp_candidates(project_id, cid, result["candidates"])
        unknowns = self._stamp_unknowns(project_id, cid, result["unknowns"])
        payload = {
            "condition_id": cid,
            "algorithm_version": ALGORITHM_VERSION,
            "param_version": param_ver,
            "param_fingerprint": state["params"].get("fingerprint")
                                 or content_hash(state["params"]["params"]),
            "skeleton_version": skeleton_ver,
            "meta_hash": meta_hash,
            "source_versions": self._active_versions(state),
            "changed_sources": changed_sources,
            "params_changed": params_changed,
            "meta_changed": meta_changed,
            "trigger_event": trigger_event,
            "candidates": candidates,
            "rejected": result["rejected"],
            "unknowns": unknowns,
            "unconvertible_peaks": result["unconvertible_peaks"],
            "missing_edge_diagnostics": result["missing_edge_diagnostics"],
            "overlap_groups": result["overlap_groups"],
            "edge_evidence_sources": result["edge_evidence_sources"],
            "expected_edges": result["expected_edges"],
        }
        event = self._append(project_id, "candidates_computed", payload,
                             self.storage.get_head(project_id, MAIN_BRANCH)
                             or trigger_event, actor)
        return {"event_id": event["event_id"], "condition_id": cid,
                "changed_sources": changed_sources,
                "n_candidates": len(candidates), "n_unknowns": len(unknowns)}

    @staticmethod
    def _active_versions(state: Dict[str, Any]) -> Dict[str, int]:
        return {kind: max(recs.keys())
                for kind, recs in state["source_versions"].items()}

    @staticmethod
    def _stamp_candidates(project_id: str, condition_id: str,
                          candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for c in candidates:
            out.append({**c, "condition_id": condition_id,
                        "stable_id": candidate_id(
                            project_id, c["peak_key"], c["nucleus"],
                            c["atom_label"], condition_id)})
        return out

    @staticmethod
    def _stamp_unknowns(project_id: str, condition_id: str,
                        unknowns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{**u, "condition_id": condition_id,
                 "stable_id": unknown_id(project_id, u["atom_label"],
                                         u["nucleus"], condition_id)}
                for u in unknowns]

    # ------------------------------------------------------------------
    # 人工裁定 / 冲突 / 撤销 / 检出（全部入历史）
    # ------------------------------------------------------------------
    def decide(self, project_id: str, candidate_stable_id: str, verdict: str,
               actor: str, note: str = "", new_branch: Optional[str] = None,
               acknowledge_ambiguity: bool = False,
               base_event_id: Optional[str] = None) -> Dict[str, Any]:
        """接受/否定一个候选。

        - 重叠峰或并列最优候选必须显式 ``acknowledge_ambiguity``，系统不替用户
          把歧义强制唯一化；决定仍记录为 provisional_ambiguous；
        - 同一峰在同一条件下已有异向接受 -> 冲突，写入 conflict_detected，
          新决定落在新分支（两位编辑者冲突）；
        - 每次裁定生成新事件（=新分支点），并携带传播影响。
        """
        if verdict not in ("accepted", "rejected"):
            raise EngineError("verdict 必须是 accepted 或 rejected")
        view = self.view_at(project_id, base_event_id) if base_event_id \
            else self.view(project_id)
        target = None
        for c in view["candidates"]:
            if c["stable_id"] == candidate_stable_id:
                target = c
                break
        if target is None:
            raise EngineError(f"候选不存在或当前不可见: {candidate_stable_id}")
        if not target["plausible"]:
            raise EngineError("不能接受已拒绝（容差外）候选")

        ambiguous = bool(target["overlap_group"]) or target["tied"]
        if ambiguous and not acknowledge_ambiguity:
            raise EngineError(
                "该候选位于重叠峰/并列最优集合中，不能被强制唯一匹配；"
                "如确有外部证据，请显式 acknowledge_ambiguity=true")

        condition_id = target["condition_id"]
        peak_key = target["peak_key"]
        prior = [d for d in view["decisions"]
                 if not d.get("undone") and d.get("peak_key") == peak_key
                 and d.get("condition_id") == condition_id
                 and d["verdict"] == "accepted"]
        conflict_with = None
        if prior and verdict == "accepted":
            conflict_with = prior[-1]

        parent = base_event_id or self.head_of(project_id, view["branch_id"])
        branch_id = new_branch or (f"review-{actor}-{peak_key}"
                                   if conflict_with else view["branch_id"])
        impacted = self._impact(view, target, verdict)
        payload = {
            "candidate_stable_id": candidate_stable_id,
            "peak_key": peak_key,
            "nucleus": target["nucleus"],
            "atom_label": target["atom_label"],
            "condition_id": condition_id,
            "verdict": verdict,
            "actor": actor,
            "note": note,
            "ambiguous": ambiguous,
            "provisional": ambiguous,
            "impact": impacted,
            "parent_view_event": view["head_event_id"],
        }
        event = self._append(project_id, "decision_made", payload, parent,
                             actor, branch_id=branch_id)
        payload["decision_stable_id"] = decision_id(project_id, event["fingerprint"])
        result = {"event": event, "decision": payload}

        if conflict_with:
            conflict_payload = {
                "peak_key": peak_key,
                "condition_id": condition_id,
                "atom_a": conflict_with["atom_label"],
                "atom_b": target["atom_label"],
                "actor_a": conflict_with["actor"],
                "actor_b": actor,
                "decision_a": conflict_with.get("decision_stable_id"),
                "decision_b": payload["decision_stable_id"],
                "status": "open",
                "branches": [conflict_with.get("branch_id", MAIN_BRANCH), branch_id],
            }
            conflict_event = self._append(
                project_id, "conflict_detected", conflict_payload,
                event["event_id"], actor, branch_id=branch_id)
            result["conflict"] = {"event": conflict_event, **conflict_payload}
        return result

    def resolve_conflict(self, project_id: str, conflict_event_id: str,
                         resolution: str, actor: str,
                         winning_decision_id: Optional[str] = None,
                         branch_id: Optional[str] = None,
                         base_event_id: Optional[str] = None) -> Dict[str, Any]:
        if resolution not in ("keep_a", "keep_b", "keep_both_provisional", "reopen"):
            raise EngineError("未知 resolution")
        conflict = self.storage.get_event(conflict_event_id)
        if not conflict or conflict["kind"] != "conflict_detected":
            raise EngineError("冲突事件不存在")
        parent = base_event_id or self.head_of(project_id, conflict["branch_id"])
        payload = {**conflict["payload"], "resolution": resolution,
                   "winning_decision_id": winning_decision_id,
                   "resolved_by": actor, "conflict_event_id": conflict_event_id}
        event = self._append(project_id, "conflict_resolved", payload, parent,
                             actor, branch_id=branch_id or conflict["branch_id"])
        return {"event": event, "resolution": payload}

    def undo_decision(self, project_id: str, decision_event_id: str, actor: str,
                      note: str = "", base_event_id: Optional[str] = None) -> Dict[str, Any]:
        decision = self.storage.get_event(decision_event_id)
        if not decision or decision["kind"] != "decision_made":
            raise EngineError("决定事件不存在")
        view = self.view_at(project_id, base_event_id) if base_event_id \
            else self.view(project_id)
        parent = base_event_id or self.head_of(project_id, decision["branch_id"])
        payload = {"undo_of_event": decision_event_id,
                   "candidate_stable_id": decision["payload"]["candidate_stable_id"],
                   "peak_key": decision["payload"]["peak_key"],
                   "condition_id": decision["payload"]["condition_id"],
                   "actor": actor, "note": note,
                   "impact": {"restored_peak": decision["payload"]["peak_key"]}}
        event = self._append(project_id, "decision_undone", payload, parent,
                             actor, branch_id=decision["branch_id"],
                             undo_of=decision_event_id)
        return {"event": event}

    def checkout(self, project_id: str, event_id: str,
                 branch_id: str = MAIN_BRANCH) -> Dict[str, Any]:
        event = self.storage.get_event(event_id)
        if not event or event["project_id"] != project_id:
            raise EngineError("事件不存在")
        self.storage.set_head(project_id, branch_id, event_id)
        return {"project_id": project_id, "branch_id": branch_id, "head_event_id": event_id}

    # ------------------------------------------------------------------
    # 折叠与视图
    # ------------------------------------------------------------------
    def require_root(self, project_id: str) -> Dict[str, Any]:
        events = self.storage.all_events(project_id)
        if not events:
            raise EngineError(f"项目不存在: {project_id}")
        return events[0]

    def head_of(self, project_id: str, branch_id: str = MAIN_BRANCH) -> Optional[str]:
        return self.storage.get_head(project_id, branch_id)

    def _ancestral_chain(self, project_id: str, head_event_id: str) -> List[Dict[str, Any]]:
        by_id = {e["event_id"]: e for e in self.storage.all_events(project_id)}
        chain: List[Dict[str, Any]] = []
        cursor: Optional[str] = head_event_id
        seen = set()
        while cursor:
            if cursor in seen:
                raise EngineError("事件 parent 链出现环")
            seen.add(cursor)
            event = by_id.get(cursor)
            if event is None:
                raise EngineError(f"父事件缺失: {cursor}")
            chain.append(event)
            cursor = event["parent_id"]
        chain.reverse()
        return chain

    def fold(self, project_id: str, head_event_id: Optional[str] = None
             ) -> Dict[str, Any]:
        events = self.storage.all_events(project_id)
        if not events:
            raise EngineError(f"项目不存在: {project_id}")
        head = head_event_id or self.storage.get_head(project_id, MAIN_BRANCH) \
            or events[0]["event_id"]
        chain_ids = {e["event_id"] for e in self._ancestral_chain(project_id, head)}
        state = new_state(project_id, events[0]["payload"]["name"])
        state["head_event_id"] = head

        for event in events:
            if event["event_id"] not in chain_ids:
                continue
            kind = event["kind"]
            p = event["payload"]
            if kind == "source_submitted":
                src = {**p["source"], "introduced_event": event["event_id"]}
                state["source_versions"][src["kind"]][src["file_version"]] = src
            elif kind == "params_revised":
                state["params"] = {"version": p["version"], "params": p["params"],
                                   "parent_version": p["parent_version"],
                                   "reason": p["reason"],
                                   "fingerprint": p["fingerprint"]}
            elif kind == "candidates_computed":
                state["snapshots"].append({**p, "event_id": event["event_id"],
                                           "clock": event["clock"]})
            elif kind == "decision_made":
                state["decisions"].append({**p, "event_id": event["event_id"],
                                           "branch_id": event["branch_id"],
                                           "clock": event["clock"],
                                           "decision_stable_id":
                                               decision_id(project_id,
                                                           event["fingerprint"]),
                                           "undone": False})
            elif kind == "decision_undone":
                for d in state["decisions"]:
                    if d["event_id"] == p["undo_of_event"]:
                        d["undone"] = True
                state["decisions"].append({
                    "event_id": event["event_id"], "branch_id": event["branch_id"],
                    "clock": event["clock"], "kind": "undo",
                    "undo_of_event": p["undo_of_event"], "actor": p["actor"],
                    "undone": False})
            elif kind == "conflict_detected":
                state["conflicts"].append({**p, "event_id": event["event_id"],
                                           "branch_id": event["branch_id"],
                                           "status": p["status"]})
            elif kind == "conflict_resolved":
                for c in state["conflicts"]:
                    if c["event_id"] == p["conflict_event_id"]:
                        c["status"] = ("open" if p["resolution"] == "reopen"
                                       else "resolved")
                        c["resolution"] = p["resolution"]
        return state

    def view(self, project_id: str, branch_id: str = MAIN_BRANCH) -> Dict[str, Any]:
        head = self.storage.get_head(project_id, branch_id)
        if head is None:
            heads = self.storage.list_heads(project_id)
            if not heads:
                raise EngineError(f"项目不存在: {project_id}")
            head = heads[0]["head_event_id"]
            branch_id = heads[0]["branch_id"]
        return self.view_at(project_id, head, branch_id=branch_id)

    def view_at(self, project_id: str, event_id: str,
                branch_id: str = MAIN_BRANCH) -> Dict[str, Any]:
        state = self.fold(project_id, event_id)
        return self._assemble_view(state, event_id, branch_id)

    def _assemble_view(self, state: Dict[str, Any], head_event_id: str,
                       branch_id: str) -> Dict[str, Any]:
        project_id = state["project_id"]
        sources = self._latest_records(state)
        material = self._current_material(state) or {}

        # 当前快照 = 链上最后一次 candidates_computed；旧快照标记 stale 可查看
        current_snapshot = state["snapshots"][-1] if state["snapshots"] else None
        current_condition = current_snapshot["condition_id"] if current_snapshot else None

        active_decisions = [d for d in state["decisions"]
                            if d.get("kind") != "undo" and not d.get("undone")]
        accepted_by_peak: Dict[Tuple[str, str], Dict[str, Any]] = {}
        rejected_ids: Dict[str, str] = {}
        for d in active_decisions:
            if d["verdict"] == "accepted":
                accepted_by_peak[(d["peak_key"], d["condition_id"])] = d
            else:
                rejected_ids[d["candidate_stable_id"]] = d.get("note", "")

        candidates: List[Dict[str, Any]] = []
        stale_candidates: List[Dict[str, Any]] = []
        if state["snapshots"]:
            for snap in state["snapshots"]:
                bucket = candidates if snap is current_snapshot else stale_candidates
                for c in snap["candidates"]:
                    accepted = accepted_by_peak.get((c["peak_key"], c["condition_id"]))
                    status = "candidate"
                    decided_by = None
                    if c["stable_id"] in rejected_ids:
                        status = "rejected_by_reviewer"
                        dec = next(d for d in active_decisions
                                   if d["candidate_stable_id"] == c["stable_id"])
                        decided_by = dec["actor"]
                    elif accepted:
                        status = "assigned" if accepted["atom_label"] == c["atom_label"] \
                            else "superseded_by_assignment"
                        decided_by = accepted["actor"]
                    bucket.append({
                        **c, "status": status, "decided_by": decided_by,
                        "snapshot_event_id": snap["event_id"],
                        "current": snap is current_snapshot,
                        "provisional": bool(accepted and accepted.get("provisional")),
                    })

        unknowns = []
        if current_snapshot:
            unknowns = [{**u, "snapshot_event_id": current_snapshot["event_id"]}
                        for u in current_snapshot["unknowns"]]

        overlap_groups = current_snapshot["overlap_groups"] if current_snapshot else {}
        missing_edges = current_snapshot["missing_edge_diagnostics"] \
            if current_snapshot else []
        rejected = current_snapshot["rejected"] if current_snapshot else []
        unconvertible = current_snapshot["unconvertible_peaks"] \
            if current_snapshot else {}

        events_timeline = self._timeline(project_id, head_event_id)

        return {
            "project_id": project_id,
            "name": state["name"],
            "branch_id": branch_id,
            "head_event_id": head_event_id,
            "algorithm_version": ALGORITHM_VERSION,
            "params": state["params"],
            "sources": [
                {"stable_id": s["stable_id"], "kind": s["kind"],
                 "filename": s["filename"], "file_version": s["file_version"],
                 "parent_source_id": s.get("parent_source_id"),
                 "digest": s["digest"], "introduced_event": s["introduced_event"]}
                for s in sorted(sources.values(),
                                key=lambda x: (x["file_version"], x["kind"]))],
            "peaks": material.get("peaks", []),
            "edges": material.get("edges", []),
            "atoms": material.get("h_atoms", []) + material.get("c_atoms", []),
            "bonds": material.get("bonds", []),
            "experiment_meta": material.get("meta"),
            "condition_id": current_condition,
            "current_snapshot_event": current_snapshot["event_id"]
                if current_snapshot else None,
            "snapshot_history": [
                {"event_id": s["event_id"], "clock": s["clock"],
                 "condition_id": s["condition_id"],
                 "param_version": s["param_version"],
                 "source_versions": s["source_versions"],
                 "changed_sources": s["changed_sources"],
                 "params_changed": s["params_changed"],
                 "meta_changed": s["meta_changed"],
                 "n_candidates": len(s["candidates"]),
                 "stale": s is not current_snapshot}
                for s in state["snapshots"]],
            "candidates": candidates,
            "stale_candidates": stale_candidates,
            "rejected_candidates": rejected,
            "unknowns": unknowns,
            "unconvertible_peaks": unconvertible,
            "missing_edge_diagnostics": missing_edges,
            "overlap_groups": overlap_groups,
            "edge_evidence_sources": current_snapshot["edge_evidence_sources"]
                if current_snapshot else {},
            "expected_edges": current_snapshot["expected_edges"]
                if current_snapshot else {"COSY": [], "HSQC": [], "HMBC": []},
            "decisions": active_decisions,
            "conflicts": state["conflicts"],
            "events": events_timeline,
            "branches": self.storage.list_heads(project_id),
        }

    def _timeline(self, project_id: str, head_event_id: str) -> List[Dict[str, Any]]:
        chain = self._ancestral_chain(project_id, head_event_id)
        out = []
        for e in chain:
            summary = {
                "project_created": "项目创建",
                "source_submitted": f"源提交 {e['payload'].get('source', {}).get('kind')} v{e['payload'].get('source', {}).get('file_version')}",
                "params_revised": f"参数修订 v{e['payload'].get('version')}: {e['payload'].get('reason')}",
                "candidates_computed": f"候选计算（{len(e['payload'].get('candidates', []))} 个合法候选）",
                "decision_made": f"{e['payload'].get('actor')} {e['payload'].get('verdict')} {e['payload'].get('peak_key')}->{e['payload'].get('atom_label')}",
                "decision_undone": f"撤销 {e['payload'].get('undo_of_event')}",
                "conflict_detected": f"冲突：{e['payload'].get('actor_a')} vs {e['payload'].get('actor_b')} @ {e['payload'].get('peak_key')}",
                "conflict_resolved": f"冲突解决：{e['payload'].get('resolution')}",
            }.get(e["kind"], e["kind"])
            out.append({"event_id": e["event_id"], "seq": e["seq"],
                        "kind": e["kind"], "parent_id": e["parent_id"],
                        "branch_id": e["branch_id"], "actor": e["actor"],
                        "clock": e["clock"], "summary": summary,
                        "undo_of": e.get("undo_of")})
        return out

    def _impact(self, view: Dict[str, Any], target: Dict[str, Any],
                verdict: str) -> Dict[str, Any]:
        """计算裁定传播影响：同峰其他候选、重叠组、对称等价原子。"""
        same_peak = [c["atom_label"] for c in view["candidates"]
                     if c["peak_key"] == target["peak_key"]
                     and c["stable_id"] != target["stable_id"]
                     and c["status"] in ("candidate",)]
        overlap_peaks: List[str] = []
        if target.get("overlap_group"):
            overlap_peaks = [
                pk for pk in view["overlap_groups"].get(target["overlap_group"], [])
                if pk != target["peak_key"]]
        equivalent = target.get("symmetry_equivalent_to") or []
        effect = ("assignment_confirmed" if verdict == "accepted"
                  else "candidate_rejected")
        return {
            "effect": effect,
            "peak_key": target["peak_key"],
            "atom_label": target["atom_label"],
            "other_candidates_on_peak": same_peak,
            "overlap_partner_peaks": overlap_peaks,
            "symmetry_equivalent_atoms": equivalent,
            "provisional": bool(target.get("overlap_group") or target.get("tied")),
            "note": ("耦合网络与重叠峰只作证据；传播影响是提示而非强制唯一化"
                     if overlap_peaks or target.get("tied") else
                     "同峰其余候选在当前分支中被标记为 superseded"),
        }
