"""服务层：项目、源版本、参数、计算、人工决定、冲突、撤销、导出/导入。"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from .assign import ALGORITHM_VERSION, compute_candidates, param_fingerprint
from .ingest import INGEST_VERSION, IngestError, parse_source, sha256_text
from .state import fold
from .store import EventStore

ARCHIVE_FORMAT = "nmr-workbench-archive/1"


class ProjectError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def project_id_for(name: str) -> str:
    return "prj-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


class ProjectService:
    def __init__(self, store: EventStore):
        self.store = store

    # ---------- 基础 ----------
    def create_project(self, name: str, narrative: str = "", actor: str = "system") -> str:
        pid = project_id_for(name)
        if self.store.events(pid):
            raise ProjectError("PROJECT_EXISTS", f"项目 {name!r} 已存在")
        self.store.append(pid, "project_created",
                          {"project_id": pid, "name": name, "narrative": narrative},
                          actor=actor)
        return pid

    def _require_project(self, project: str) -> None:
        if not self.store.events(project):
            raise ProjectError("PROJECT_NOT_FOUND", f"项目 {project} 不存在")

    def state(self, project: str, as_of: Optional[int] = None) -> dict[str, Any]:
        self._require_project(project)
        return fold(self.store.events(project, upto_seq=as_of))

    def events(self, project: str) -> list[dict[str, Any]]:
        self._require_project(project)
        return self.store.events(project)

    # ---------- 源提交（不可覆盖，只新增版本） ----------
    def commit_source(self, project: str, source_key: str, raw_text: str,
                      actor: str = "system", note: str = "") -> dict[str, Any]:
        self._require_project(project)
        before = self.state(project)
        parsed = parse_source(raw_text)  # 硬错误抛 IngestError
        versions = before["sources"].get(source_key, {}).get("versions", [])
        version_id = f"{source_key}@v{len(versions) + 1}"
        parent = versions[-1] if versions else None
        head = before["head_event"]
        ev1 = self.store.append(project, "source_committed", {
            "source_key": source_key,
            "version_id": version_id,
            "parent_version_id": parent,
            "kind": parsed["kind"],
            "meta": parsed["meta"],
            "sha256": sha256_text(raw_text),
            "raw_text": raw_text,
            "note": note,
        }, actor=actor, parents=[head] if head else [])
        ev2 = self.store.append(project, "ingest_completed", {
            "source_key": source_key,
            "version_id": version_id,
            "algorithm": INGEST_VERSION,
            "derived_from": [ev1["event_id"]],
            "parsed": parsed,
        }, actor=actor, parents=[ev1["event_id"]])
        after = self.state(project)
        return {
            "events": [ev1, ev2],
            "version_id": version_id,
            "diagnostics": parsed["diagnostics"],
            "impact": self._stale_impact(before, after),
        }

    def commit_params(self, project: str, params: dict[str, Any],
                      actor: str = "system", note: str = "") -> dict[str, Any]:
        self._require_project(project)
        before = self.state(project)
        versions = before["params"]["versions"]
        version_id = f"params@v{len(versions) + 1}"
        parent = versions[-1]["version_id"] if versions else None
        head = before["head_event"]
        ev = self.store.append(project, "params_committed", {
            "version_id": version_id,
            "parent_version_id": parent,
            "params": params,
            "fingerprint": param_fingerprint(params),
            "note": note,
        }, actor=actor, parents=[head] if head else [])
        after = self.state(project)
        return {"events": [ev], "version_id": version_id,
                "fingerprint": param_fingerprint(params),
                "impact": self._stale_impact(before, after)}

    # ---------- 候选计算 ----------
    def compute(self, project: str, actor: str = "system") -> dict[str, Any]:
        self._require_project(project)
        before = self.state(project)
        if not before["params"]["current"]:
            raise ProjectError("PARAMS_MISSING", "尚未提交参数，无法计算候选")
        params = before["params"]["versions"][-1]["params"]

        skeleton = None
        h1: list[dict] = []
        c13: list[dict] = []
        edges: list[dict] = []
        depends_on: dict[str, str] = {}
        for key, entry in before["sources"].items():
            vid = entry["current"]
            parsed = before["ingests"][vid]
            depends_on[key] = vid
            if parsed["kind"] == "skeleton":
                skeleton = parsed
            elif parsed["kind"] == "peaklist-1h":
                h1 = parsed["peaks"]
            elif parsed["kind"] == "peaklist-13c":
                c13 = parsed["peaks"]
            elif parsed["kind"] == "correlations":
                edges = parsed["edges"]
        if skeleton is None:
            raise ProjectError("SKELETON_MISSING", "尚未提交骨架（skeleton）源")

        result = compute_candidates(skeleton, h1, c13, edges, params)
        set_id = f"cs-{len(before['candidate_sets']) + 1:03d}"
        head = before["head_event"]
        ev = self.store.append(project, "candidates_computed", {
            "set_id": set_id,
            "algorithm_version": ALGORITHM_VERSION,
            "param_fingerprint": param_fingerprint(params),
            "params_version": before["params"]["current"],
            "depends_on": depends_on,
            "result": result,
        }, actor=actor, parents=[head] if head else [])
        after = self.state(project)
        return {"events": [ev], "set_id": set_id, "result": result,
                "impact": self._stale_impact(before, after)}

    # ---------- 人工决定 ----------
    def decide(self, project: str, actor: str, target_candidate_id: str,
               target_set_id: str, verdict: str, rationale: str = "",
               judgment_basis: Optional[list[str]] = None,
               base_event_id: Optional[str] = None,
               branch: Optional[str] = None) -> dict[str, Any]:
        self._require_project(project)
        if verdict not in ("confirm", "reject", "adjust"):
            raise ProjectError("BAD_VERDICT", f"未知裁定 {verdict!r}")
        events = self.store.events(project)
        head = events[-1]
        base_event_id = base_event_id or head["event_id"]
        base = self.store.get(base_event_id)
        if base is None or base["project"] != project:
            raise ProjectError("BAD_BASE", f"基准事件 {base_event_id} 不存在")

        events_out: list[dict[str, Any]] = []
        if branch is None:
            branch = base["branch"]
            branch_head = None
            for e in reversed(events):
                if e["branch"] == branch:
                    branch_head = e
                    break
            if branch_head and branch_head["event_id"] != base_event_id:
                # 从旧事件出发裁定 -> 生成新分支
                n = len([e for e in events if e["kind"] == "branch_created"]) + 1
                branch = f"br-{n}"
                ev = self.store.append(project, "branch_created", {
                    "branch": branch, "from_event": base_event_id,
                    "reason": "从旧事件出发的人工决定生成新分支",
                }, actor=actor, parents=[base_event_id])
                events_out.append(ev)

        target_id = self._target_of_candidate(project, target_set_id, target_candidate_id)
        decision_id = f"dc-{base['seq']:06d}-{actor}"
        ev = self.store.append(project, "decision_made", {
            "decision_id": decision_id,
            "target_candidate_id": target_candidate_id,
            "target_id": target_id,
            "target_set_id": target_set_id,
            "verdict": verdict,
            "rationale": rationale,
            "judgment_basis": judgment_basis or [],
        }, actor=actor, branch=branch, parents=[base_event_id])
        events_out.append(ev)

        # 冲突检测：同一候选、不同裁定、不同编辑者、互不祖先
        conflicts_out: list[dict[str, Any]] = []
        st = self.state(project)
        for other in st["decisions"]:
            if other["decision_id"] == decision_id or not other["effective"]:
                continue
            if other.get("target_id") != target_id:
                continue
            if other["actor"] == actor:
                continue
            incompatible = (other["verdict"] != verdict) or (
                verdict == "confirm"
                and other["target_candidate_id"] != target_candidate_id)
            if not incompatible:
                continue
            if self.store.is_ancestor(project, ev["event_id"], other["event_id"]):
                continue
            if self.store.is_ancestor(project, other["event_id"], ev["event_id"]):
                continue
            pair = sorted([other["decision_id"], decision_id])
            already = any(
                c["status"] == "open" and set(c["decision_ids"]) == set(pair)
                for c in st["conflicts"])
            if not already:
                cid = f"cf-{len(st['conflicts']) + 1:03d}"
                cev = self.store.append(project, "conflict_recorded", {
                    "conflict_id": cid,
                    "decision_ids": pair,
                    "target_id": target_id,
                    "target_candidate_id": target_candidate_id,
                    "actors": sorted({other["actor"], actor}),
                }, actor="system", parents=[ev["event_id"]])
                events_out.append(cev)
                conflicts_out.append(cev)

        after = self.state(project)
        impact = self._decision_impact(after, target_set_id, target_candidate_id, verdict)
        impact["new_branch"] = branch if events_out and events_out[0]["kind"] == "branch_created" else None
        impact["conflicts"] = [c["payload"]["conflict_id"] for c in conflicts_out]
        return {"events": events_out, "decision_id": decision_id, "branch": branch,
                "impact": impact}

    def revert_decision(self, project: str, decision_id: str, actor: str = "system",
                        reason: str = "") -> dict[str, Any]:
        self._require_project(project)
        st = self.state(project)
        if not any(d["decision_id"] == decision_id for d in st["decisions"]):
            raise ProjectError("DECISION_NOT_FOUND", f"决定 {decision_id} 不存在")
        head = st["head_event"]
        ev = self.store.append(project, "decision_reverted",
                               {"reverts": decision_id, "reason": reason},
                               actor=actor, parents=[head])
        return {"events": [ev]}

    def resolve_conflict(self, project: str, conflict_id: str, chosen_decision_id: str,
                         actor: str = "system", rationale: str = "") -> dict[str, Any]:
        self._require_project(project)
        st = self.state(project)
        conflict = next((c for c in st["conflicts"] if c["conflict_id"] == conflict_id), None)
        if conflict is None:
            raise ProjectError("CONFLICT_NOT_FOUND", f"冲突 {conflict_id} 不存在")
        if conflict["status"] != "open":
            raise ProjectError("CONFLICT_CLOSED", f"冲突 {conflict_id} 已解决")
        if chosen_decision_id not in conflict["decision_ids"]:
            raise ProjectError("BAD_CHOICE", "所选决定不在冲突集合内")
        head = st["head_event"]
        ev = self.store.append(project, "conflict_resolved", {
            "conflict_id": conflict_id,
            "chosen_decision_id": chosen_decision_id,
            "rationale": rationale,
        }, actor=actor, parents=[head])
        return {"events": [ev]}

    # ---------- 导出 / 导入 ----------
    def export(self, project: str) -> dict[str, Any]:
        self._require_project(project)
        return {
            "format": ARCHIVE_FORMAT,
            "project": project,
            "events": self.store.events(project),
        }

    def import_archive(self, archive: dict[str, Any]) -> str:
        if archive.get("format") != ARCHIVE_FORMAT:
            raise ProjectError("BAD_ARCHIVE", f"归档格式必须是 {ARCHIVE_FORMAT}")
        project = archive["project"]
        if self.store.events(project):
            raise ProjectError("PROJECT_EXISTS", f"项目 {project} 已存在，拒绝覆盖导入")
        rows = sorted(archive["events"], key=lambda r: r["seq"])
        for i, row in enumerate(rows):
            if row["seq"] != i + 1:
                raise ProjectError("BAD_ARCHIVE", "归档事件序列不连续")
            self.store.insert_row(row)
        return project

    def _target_of_candidate(self, project: str, set_id: str, candidate_id: str) -> str:
        st = self.state(project)
        for cs in st["candidate_sets"]:
            if cs["set_id"] != set_id:
                continue
            for t in cs["result"]["targets"]:
                for c in t["candidates"]:
                    if c["candidate_id"] == candidate_id:
                        return t["target_id"]
        raise ProjectError("BAD_CANDIDATE",
                           f"候选 {candidate_id} 不在候选集 {set_id} 中")

    # ---------- 影响分析 ----------
    @staticmethod
    def _stale_impact(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        before_stale = {cs["set_id"] for cs in before["candidate_sets"] if cs["stale"]}
        newly = []
        for cs in after["candidate_sets"]:
            if cs["stale"] and cs["set_id"] not in before_stale:
                newly.append({"set_id": cs["set_id"], "reasons": cs["stale_reasons"]})
        return {"newly_stale_sets": newly}

    @staticmethod
    def _decision_impact(state: dict[str, Any], set_id: str,
                         candidate_id: str, verdict: str) -> dict[str, Any]:
        affected: list[dict[str, Any]] = []
        for cs in state["candidate_sets"]:
            if cs["set_id"] != set_id:
                continue
            for t in cs["result"]["targets"]:
                for c in t["candidates"]:
                    if c["candidate_id"] == candidate_id:
                        affected.append({"candidate_id": c["candidate_id"],
                                         "decision_status": c["decision_status"]})
                    elif verdict == "confirm" and any(
                            x["candidate_id"] == candidate_id for x in t["candidates"]):
                        if c["decision_status"] in ("displaced", "rejected"):
                            affected.append({"candidate_id": c["candidate_id"],
                                             "decision_status": c["decision_status"]})
        return {"affected_candidates": affected}
