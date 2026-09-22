"""应用服务：把存储、解析、规范化和领域引擎串成可重放工作流。"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Tuple

from .assign import DEFAULT_PARAMS, run_for_both_nuclei
from .normalize import build_edges, build_peaks, build_sites, rows_from_docs
from .parser import parse_text
from .repository import Repo


class ProjectService:
    def __init__(self, repo: Repo):
        self.repo = repo

    # ---------- 导入 ----------
    def import_source(self, project_id: str, logical_name: str, text: str,
                      actor: str, change_note: str, parent_event: Optional[str] = None,
                      branch: str = "main") -> Dict[str, Any]:
        parsed = parse_text(logical_name, text)
        committed = self.repo.commit_document(project_id, parsed, actor, change_note,
                                              parent_event, branch)
        recomputed = self._recompute(project_id, parent_event=committed["event"]["id"],
                                     actor=actor, branch=branch,
                                     reason=f"导入/修订 {logical_name} v{committed['version']}")
        committed["commit_event"] = committed["event"]
        committed["event"] = recomputed.get("event", committed["event"])
        return committed

    def revise_params(self, project_id: str, params: Dict[str, Any], actor: str,
                      note: str, parent_event: Optional[str] = None,
                      branch: str = "main") -> Dict[str, Any]:
        out = self.repo.revise_params(project_id, params, actor, note, parent_event, branch)
        recomputed = self._recompute(project_id, parent_event=out["event"]["id"],
                                     actor=actor, branch=branch,
                                     reason=f"参数修订 v{out['version']}：{note}")
        out["commit_event"] = out["event"]
        out["event"] = recomputed.get("event", out["event"])
        return out

    def change_conditions(self, project_id: str, logical_name: str, change_kind: str,
                          new_text: str, actor: str, parent_event: Optional[str],
                          branch: str = "main") -> Dict[str, Any]:
        """参考/溶剂/温度变更：以新版本提交，只使相关核候选过期。"""
        old_docs = [d for d in self.repo.list_documents(project_id)
                    if d["logical_name"] == logical_name]
        old_sig = self._conditions_signature(old_docs[0]["headers"]) if old_docs else ""
        committed = self.import_source(project_id, logical_name, new_text, actor,
                                       f"条件变更：{change_kind}", parent_event, branch)
        new_sig = self._conditions_signature(
            parse_text(logical_name, new_text).headers)
        nucleus = parse_text(logical_name, new_text).headers.get("nucleus", "")
        evt = self.repo.conditions_change_event(
            project_id, committed["doc_id"], change_kind, old_sig, new_sig, nucleus,
            actor, committed["event"]["id"], branch)
        return {"commit": committed, "event": evt, "commit_event": committed["commit_event"]}

    @staticmethod
    def _conditions_signature(headers: Dict[str, str]) -> str:
        return "|".join(headers.get(k, "") for k in
                        ("solvent", "temperature-k", "reference", "nucleus"))

    # ---------- 计算（内容寻址、去重、局部失效） ----------
    def _assemble(self, project_id: str, up_to_event: Optional[str]):
        docv, params_version, cond = self.repo.latest_versions(project_id, up_to_event)
        docs = self.repo.get_documents_at(project_id, docv)
        grouped = rows_from_docs(docs)
        peaks, edges, sites, expected = [], [], [], []
        peak_docs_meta = []
        for doc, rows in grouped["peak_docs"]:
            peaks.extend(build_peaks(rows, doc["headers"]))
            peak_docs_meta.append({"doc_id": doc["doc_id"], "version": doc["version"],
                                   "headers": doc["headers"]})
        for doc, rows in grouped["edge_docs"]:
            edges.extend(build_edges(rows, doc["headers"]))
        for doc, rows in grouped["skel_docs"]:
            s, e = build_sites(rows)
            sites.extend(s)
            expected.extend(e)
        params = self.repo.get_params(project_id, params_version)
        return params, params_version, peaks, edges, sites, expected, docs, peak_docs_meta

    def _recompute(self, project_id: str, parent_event: Optional[str], actor: str,
                   branch: str, reason: str) -> Dict[str, Any]:
        params, params_version, peaks, edges, sites, expected, docs, meta = \
            self._assemble(project_id, parent_event)
        if not sites:
            return {"results": [], "reason": reason}
        doc_versions = {d["doc_id"]: d["version"] for d in docs}
        results = run_for_both_nuclei(sites, peaks, edges, expected, params, doc_versions)
        saved = []
        for nucleus, result in results.items():
            self.repo.save_result(project_id, result, params_version, parent_event)
            saved.append(result.id)
        evt = self.repo.add_event(project_id, "candidates_computed",
                                  {"result_ids": saved, "reason": reason,
                                   "params_version": params_version,
                                   "doc_versions": doc_versions,
                                   "findings": {n: [f.as_dict() for f in r.findings]
                                                for n, r in results.items()}},
                                  actor, parent_event, branch)
        return {"results": saved, "event": evt, "reason": reason}

    def recompute_after_decision(self, project_id: str, parent_event: str,
                                 actor: str, branch: str) -> Optional[str]:
        out = self._recompute(project_id, parent_event, actor, branch,
                              "人工裁定后重算候选（决定不改变计算，只记录传播）")
        return out.get("event", {}).get("id") if out.get("event") else None

    # ---------- 状态快照 ----------
    def snapshot(self, project_id: str, at_event: Optional[str] = None) -> Dict[str, Any]:
        """组装某一历史事件时点的完整可见状态（不拿最新结果覆盖历史）。"""
        events = self.repo.list_events(project_id)
        if at_event is None and events:
            at_event = events[-1]["id"]
        chain_ids = set(self.repo.ancestors(project_id, at_event))
        chain = [e for e in events if e["id"] in chain_ids and not e["undone"]]
        head_id = at_event
        docv, params_version, cond_changes = self.repo.latest_versions(project_id, at_event)
        params = self.repo.get_params(project_id, params_version)
        docs = self.repo.get_documents_at(project_id, docv)

        # 该时点“最新计算”结果：沿链找最近一次 candidates_computed
        computed = [e for e in chain if e["kind"] == "candidates_computed"]
        latest_result_ids: List[str] = []
        if computed:
            latest_result_ids = computed[-1]["payload"]["result_ids"]

        all_results = {r["id"]: r for r in self.repo.all_results(project_id)}

        # 当前输入下若现在重算会得到什么（仅用于标注过期，不覆盖展示）
        fresh_inputs: Dict[str, Tuple[Any, ...]] = {}
        grouped = rows_from_docs(docs)
        cur_peaks, cur_edges, cur_sites, cur_expected = [], [], [], []
        for doc, rows in grouped["peak_docs"]:
            cur_peaks.extend(build_peaks(rows, doc["headers"]))
        for doc, rows in grouped["edge_docs"]:
            cur_edges.extend(build_edges(rows, doc["headers"]))
        for doc, rows in grouped["skel_docs"]:
            s, e = build_sites(rows)
            cur_sites.extend(s)
            cur_expected.extend(e)

        results_out: List[Dict[str, Any]] = []
        current_record_map = self.repo.current_record_versions(project_id, docv)
        chain_decision_ids = {e["id"] for e in chain if e["kind"] == "decision_made"}
        decisions = self.repo.list_decisions(project_id)
        adopted_by_site: Dict[str, Dict[str, Any]] = {}
        for d in decisions:
            ev = next((e for e in events if e["id"] == d["event_id"]), None)
            if ev and ev["id"] in chain_ids and not ev["undone"] and d["adopted"]:
                adopted_by_site[d["site_id"]] = d

        from .assign import run_for_both_nuclei
        from .identity import result_id, param_fingerprint
        from . import ALGO_VERSION
        if cur_sites:
            current_doc_versions = {d["doc_id"]: d["version"] for d in docs}
            fresh = run_for_both_nuclei(cur_sites, cur_peaks, cur_edges, cur_expected,
                                        params, current_doc_versions)
            fresh_ids = {n: r.id for n, r in fresh.items()}
            fresh_map = {n: r for n, r in fresh.items()}
        else:
            fresh_ids, fresh_map = {}, {}

        # 展示该时点最近一次计算的结果，并标注 live/stale
        for rid in latest_result_ids:
            stored = all_results.get(rid)
            if not stored:
                continue
            nucleus = stored["nucleus"]
            expected_now = fresh_ids.get(nucleus)
            is_current = expected_now == rid
            payload = copy.deepcopy(stored["payload"])
            payload["state"] = "current" if is_current else "stale"
            if not is_current:
                changed_deps = self._changed_dependencies(project_id, rid, current_record_map)
                payload["staleness"] = {
                    "reason": "输入记录、参数指纹或条件签名已变化",
                    "changed_dependencies": changed_deps,
                    "current_result_id": expected_now,
                }
            self._annotate_decisions(payload, adopted_by_site, decisions, chain_decision_ids)
            results_out.append(payload)

        # 历史上沿链出现、但已不是最新的结果也可查看（去重，保留首次出现事件）
        seen = {r["id"] for r in results_out}
        for e in computed[:-1]:
            for rid in e["payload"].get("result_ids", []):
                if rid in seen or rid not in all_results:
                    continue
                stored = all_results[rid]
                payload = copy.deepcopy(stored["payload"])
                payload["state"] = "historical"
                payload["first_event_id"] = e["id"]
                payload["first_event_at"] = e["at"]
                results_out.append(payload)
                seen.add(rid)
        # 该历史时点之后才产生的结果：可查看，但明确标记 stale（不能冒充当时结论）
        tip = events[-1]["id"] if events else None
        if tip and tip != head_id:
            tip_docv, tip_pv, _ = self.repo.latest_versions(project_id, tip)
            tip_docs = self.repo.get_documents_at(project_id, tip_docv)
            tg = rows_from_docs(tip_docs)
            tp, te, ts, tx = [], [], [], []
            for d, rows in tg["peak_docs"]:
                tp.extend(build_peaks(rows, d["headers"]))
            for d, rows in tg["edge_docs"]:
                te.extend(build_edges(rows, d["headers"]))
            for d, rows in tg["skel_docs"]:
                a, b = build_sites(rows)
                ts.extend(a)
                tx.extend(b)
            tparams = self.repo.get_params(project_id, tip_pv)
            for tresult in run_for_both_nuclei(ts, tp, te, tx, tparams,
                                               {d["doc_id"]: d["version"] for d in tip_docs}).values():
                if tresult.id in seen or tresult.id not in all_results:
                    continue
                payload = copy.deepcopy(all_results[tresult.id]["payload"])
                payload["state"] = "stale"
                payload["staleness"] = {"reason": "该结果在本次历史时点之后才产生，不能覆盖当时结论"}
                results_out.append(payload)
                seen.add(tresult.id)

        docs_out = []
        for d in docs:
            docs_out.append({
                "doc_id": d["doc_id"], "logical_name": d["logical_name"],
                "version": d["version"], "format": d["format"],
                "headers": d["headers"], "raw_text": d["raw_text"],
                "committed_at": d["committed_at"], "author": d["author"],
                "change_note": d["change_note"], "has_error": d["has_error"],
                "diagnostics": d["diagnostics"],
                "records": d["records"],
            })

        # 骨架与边视图
        skeleton = {"sites": [s.as_dict() for s in cur_sites],
                    "expected_edges": [e.__dict__ for e in cur_expected]}
        peaks_out = [p.as_dict() for p in cur_peaks]
        edges_out = [e.as_dict() for e in cur_edges]

        return {
            "project_id": project_id,
            "head_event": head_id,
            "params_version": params_version,
            "params": params,
            "documents": docs_out,
            "peaks": peaks_out,
            "edges": edges_out,
            "skeleton": skeleton,
            "results": results_out,
            "fresh_result_ids": fresh_ids,
            "events": events,
            "decisions": [self._decision_view(project_id, d) for d in decisions],
            "conflicts": self.repo.list_conflicts(project_id),
            "scenario_findings": self.repo.list_scenario_findings(project_id),
            "conditions_changes": [e for e in chain if e["kind"] == "conditions_changed"],
        }

    def _changed_dependencies(self, project_id: str, result_id: str,
                              current_record_map: Dict[str, Tuple[str, int]]) -> List[str]:
        changed: List[str] = []
        for rid, role in self.repo.result_deps(result_id):
            if rid not in current_record_map:
                changed.append(f"{rid} ({role}: 已变更/移除或其所属文档新版本中身份不同)")
        return sorted(set(changed))

    def _annotate_decisions(self, payload: Dict[str, Any], adopted_by_site,
                            decisions, chain_decision_ids) -> None:
        for site_id, cands in payload["candidate_map"].items():
            chosen = adopted_by_site.get(site_id)
            for cand in cands:
                cand["human"] = None
                if chosen and chosen["peak_id"] == cand["peak_id"]:
                    cand["human"] = {"verdict": chosen["verdict"], "actor": chosen["actor"],
                                     "decision_id": chosen["id"], "adopted": bool(chosen["adopted"]),
                                     "rationale": chosen["rationale"]}
                    if chosen["verdict"] == "reject":
                        cand["status"] = "rejected"
                    elif chosen["verdict"] == "accept":
                        cand["status"] = "confirmed"

    def _decision_view(self, project_id: str, d: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(d)
        ev_row = self.repo.conn.execute(
            "SELECT kind,parent_id,branch,at,actor,payload_json FROM events WHERE id=?",
            (d["event_id"],)).fetchone()
        if ev_row:
            out["event"] = {"id": d["event_id"], "kind": ev_row["kind"],
                            "parent_id": ev_row["parent_id"], "branch": ev_row["branch"],
                            "at": ev_row["at"], "actor": ev_row["actor"],
                            "payload": json.loads(ev_row["payload_json"])}
        return out

    # ---------- 人工裁定工作流 ----------
    def decide(self, project_id: str, site_id: str, peak_id: Optional[str], result_id: str,
               verdict: str, actor: str, rationale: str, parent_event: str,
               branch: Optional[str] = None) -> Dict[str, Any]:
        target_branch = branch or f"review-{actor}-{site_id.split('_')[-1][:6]}"
        out = self.repo.add_decision_event(project_id, site_id, peak_id, result_id,
                                           verdict, actor, rationale, parent_event,
                                           target_branch)
        recomputed = self._recompute(project_id, out["event"]["id"], actor,
                                     target_branch, f"{actor} 对 {site_id} 的{verdict}")
        return {"decision": out, "recomputed_event": recomputed.get("event"),
                "branch": target_branch}

    def detect_conflict(self, project_id: str, site_id: str, decision_a_id: str,
                        decision_b_id: str, event_id_: str) -> Optional[str]:
        a = self.repo.conn.execute("SELECT * FROM decisions WHERE id=?",
                                   (decision_a_id,)).fetchone()
        b = self.repo.conn.execute("SELECT * FROM decisions WHERE id=?",
                                   (decision_b_id,)).fetchone()
        if not a or not b:
            return None
        conflicting = (a["verdict"] != b["verdict"]
                       or a["peak_id"] != b["peak_id"]
                       or (a["verdict"] == "accept" and a["peak_id"] != b["peak_id"]))
        if not conflicting:
            return None
        cid = self.repo.open_conflict(project_id, site_id, decision_a_id,
                                      decision_b_id, event_id_)
        self.repo.add_scenario_finding(project_id, "EDITOR_CONFLICT", site_id,
                                       {"conflict_id": cid,
                                        "decision_a": decision_a_id,
                                        "decision_b": decision_b_id,
                                        "site_id": site_id}, event_id_)
        return cid

    def resolve(self, project_id: str, conflict_id: str, winner_decision: str,
                loser_decision: str, actor: str, rationale: str, parent_event: str,
                branch: str) -> Dict[str, Any]:
        event = self.repo.resolve_conflict(project_id, conflict_id, winner_decision,
                                           loser_decision, actor, rationale,
                                           parent_event, branch)
        recomputed = self._recompute(project_id, event["id"], actor, branch,
                                     f"冲突 {conflict_id} 解决后重算")
        return {"resolution_event": event, "recomputed_event": recomputed.get("event")}

    def undo(self, project_id: str, decision_event_id: str, actor: str,
             parent_event: str, branch: str) -> Dict[str, Any]:
        event = self.repo.undo_decision(project_id, decision_event_id, actor,
                                        parent_event, branch)
        return {"undo_event": event}
