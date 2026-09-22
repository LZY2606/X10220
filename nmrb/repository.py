"""仓库：封装事件溯源写入、版本推进、重放与派生结果失效判断。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .identity import (digest, doc_id, event_id, lineage_id, param_fingerprint,
                       record_id, stable_id)
from .parser import ParsedDocument
from .timeutil import Clock

ROOTS = ("main",)


class Repo:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    # ---------- 基础 ----------
    def create_project(self, project_id: str, name: str) -> Dict[str, Any]:
        at = self.clock.now()
        self.conn.execute("INSERT INTO projects(id,name,created_at) VALUES (?,?,?)",
                          (project_id, name, at))
        self.conn.commit()
        return {"id": project_id, "name": name, "created_at": at}

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    # ---------- 事件 ----------
    def _next_seq(self, project_id: str) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE project_id=?",
                                (project_id,)).fetchone()
        return int(row["n"])

    def add_event(self, project_id: str, kind: str, payload: Dict[str, Any],
                  actor: str, parent_id: Optional[str] = None, branch: str = "main") -> Dict[str, Any]:
        seq = self._next_seq(project_id)
        at = self.clock.now()
        eid = event_id(project_id, seq, kind, parent_id or "root", digest([payload]), at)
        self.conn.execute(
            "INSERT INTO events(id,project_id,seq,parent_id,branch,kind,actor,at,payload_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, project_id, seq, parent_id, branch, kind, actor, at,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        self.conn.commit()
        return {"id": eid, "seq": seq, "parent_id": parent_id, "branch": branch,
                "kind": kind, "actor": actor, "at": at, "payload": payload}

    def list_events(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()
        return [self._event_dict(r) for r in rows]

    def _event_dict(self, r: sqlite3.Row) -> Dict[str, Any]:
        return {"id": r["id"], "seq": r["seq"], "parent_id": r["parent_id"],
                "branch": r["branch"], "kind": r["kind"], "actor": r["actor"],
                "at": r["at"], "payload": json.loads(r["payload_json"]),
                "undone": bool(r["undone"]), "undone_by": r["undone_by"]}

    def ancestors(self, project_id: str, event_id_: Optional[str]) -> List[str]:
        """返回事件祖先链（含自身，根到叶顺序）。"""
        by_id = {e["id"]: e for e in self.list_events(project_id)}
        chain: List[str] = []
        cur = event_id_
        guard = 0
        while cur and cur in by_id and guard < 10000:
            chain.append(cur)
            cur = by_id[cur]["parent_id"]
            guard += 1
        return list(reversed(chain))

    # ---------- 原始文档（不可变版本链） ----------
    def commit_document(self, project_id: str, parsed: ParsedDocument, actor: str,
                        change_note: str, parent_event: Optional[str] = None,
                        branch: str = "main") -> Dict[str, Any]:
        d_id = doc_id(project_id, parsed.logical_name)
        l_id = lineage_id(project_id, parsed.logical_name)
        row = self.conn.execute("SELECT current_version FROM documents WHERE id=?",
                                (d_id,)).fetchone()
        if row is None:
            version, parent_version = 1, None
            self.conn.execute(
                "INSERT INTO documents(id,project_id,lineage_id,logical_name,current_version)"
                " VALUES (?,?,?,?,1)", (d_id, project_id, l_id, parsed.logical_name))
        else:
            version = int(row["current_version"]) + 1
            parent_version = int(row["current_version"])
            self.conn.execute("UPDATE documents SET current_version=? WHERE id=?",
                              (version, d_id))
        content_sha = digest([parsed.raw_text if False else ""])
        raw_text = getattr(parsed, "raw_text", None)
        at = self.clock.now()
        self.conn.execute(
            "INSERT INTO document_versions(doc_id,version,parent_version,project_id,format,"
            "raw_text,content_sha,headers_json,committed_at,author,change_note,has_error,"
            "diagnostics_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d_id, version, parent_version, project_id, parsed.format,
             raw_text if raw_text is not None else "",
             digest([raw_text or ""]), json.dumps(parsed.headers, ensure_ascii=False, sort_keys=True),
             at, actor, change_note, 1 if parsed.has_error else 0,
             json.dumps([d.as_dict() for d in parsed.diagnostics], ensure_ascii=False)))
        recs = []
        for r in parsed.records:
            rid = record_id(d_id, r.lineno, r.label, r.raw)
            self.conn.execute(
                "INSERT INTO records(id,doc_id,version,label,lineno,kind,raw,fields_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (rid, d_id, version, r.label, r.lineno, r.kind, r.raw,
                 json.dumps(r.fields, ensure_ascii=False, sort_keys=True)))
            recs.append({"id": rid, "doc_id": d_id, "doc_version": version,
                         "label": r.label, "lineno": r.lineno, "kind": r.kind,
                         "raw": r.raw, "fields": r.fields})
        self.conn.commit()
        payload = {"logical_name": parsed.logical_name, "doc_id": d_id, "version": version,
                   "parent_version": parent_version, "change_note": change_note,
                   "has_error": parsed.has_error,
                   "diagnostics": [d.as_dict() for d in parsed.diagnostics],
                   "record_ids": [r["id"] for r in recs]}
        event = self.add_event(project_id, "source_committed", payload, actor,
                               parent_event, branch)
        return {"doc_id": d_id, "version": version, "records": recs, "event": event,
                "diagnostics": [d.as_dict() for d in parsed.diagnostics]}

    def latest_versions(self, project_id: str, up_to_event: Optional[str] = None
                        ) -> Tuple[Dict[str, int], int, Dict[str, Any]]:
        """沿事件祖先链重放，得到每个文档/参数在该时点的版本。"""
        chain = set(self.ancestors(project_id, up_to_event))
        docv: Dict[str, int] = {}
        params_version = 0
        condition_basis: Dict[str, Any] = {}
        for e in self.list_events(project_id):
            if e["id"] not in chain or e["undone"]:
                continue
            p = e["payload"]
            if e["kind"] == "source_committed":
                docv[p["doc_id"]] = p["version"]
            elif e["kind"] == "params_revised":
                params_version = p["version"]
            elif e["kind"] == "conditions_changed":
                condition_basis[p.get("doc_id", "*")] = p.get("new_conditions_signature")
        return docv, params_version, condition_basis

    def get_documents_at(self, project_id: str, docv: Dict[str, int]
                         ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for d_id, version in docv.items():
            dv = self.conn.execute(
                "SELECT * FROM document_versions WHERE doc_id=? AND version=?",
                (d_id, version)).fetchone()
            if dv is None:
                continue
            recs = self.conn.execute(
                "SELECT * FROM records WHERE doc_id=? AND version=? ORDER BY lineno",
                (d_id, version)).fetchall()
            out.append({
                "doc_id": d_id, "version": version,
                "parent_version": dv["parent_version"],
                "logical_name": self.conn.execute(
                    "SELECT logical_name FROM documents WHERE id=?", (d_id,)).fetchone()["logical_name"],
                "format": dv["format"], "raw_text": dv["raw_text"],
                "headers": json.loads(dv["headers_json"]),
                "committed_at": dv["committed_at"], "author": dv["author"],
                "change_note": dv["change_note"], "has_error": bool(dv["has_error"]),
                "diagnostics": json.loads(dv["diagnostics_json"]),
                "records": [{"id": r["id"], "doc_id": d_id, "doc_version": version,
                             "label": r["label"], "lineno": r["lineno"], "kind": r["kind"],
                             "raw": r["raw"], "fields": json.loads(r["fields_json"])}
                            for r in recs],
            })
        return out

    def list_documents(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT d.id doc_id,d.logical_name,d.current_version,"
            "dv.format,dv.headers_json,dv.raw_text,dv.committed_at,dv.author,dv.change_note,"
            "dv.has_error,dv.diagnostics_json FROM documents d JOIN document_versions dv"
            " ON dv.doc_id=d.id AND dv.version=d.current_version WHERE d.project_id=?",
            (project_id,)).fetchall()
        out = []
        for r in rows:
            recs = self.conn.execute(
                "SELECT * FROM records WHERE doc_id=? AND version=? ORDER BY lineno",
                (r["doc_id"], r["current_version"])).fetchall()
            out.append({"doc_id": r["doc_id"], "logical_name": r["logical_name"],
                        "version": r["current_version"], "format": r["format"],
                        "raw_text": r["raw_text"],
                        "headers": json.loads(r["headers_json"]),
                        "committed_at": r["committed_at"], "author": r["author"],
                        "change_note": r["change_note"], "has_error": bool(r["has_error"]),
                        "diagnostics": json.loads(r["diagnostics_json"]),
                        "records": [{"id": x["id"], "doc_id": r["doc_id"],
                                     "doc_version": r["current_version"], "label": x["label"],
                                     "lineno": x["lineno"], "kind": x["kind"], "raw": x["raw"],
                                     "fields": json.loads(x["fields_json"])} for x in recs]})
        return out

    # ---------- 参数版本 ----------
    def revise_params(self, project_id: str, params: Dict[str, Any], actor: str, note: str,
                      parent_event: Optional[str] = None, branch: str = "main") -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version),0) v FROM params_versions WHERE project_id=?",
            (project_id,)).fetchone()
        version = int(row["v"]) + 1
        parent_version = version - 1 or None
        fp = param_fingerprint(params)
        at = self.clock.now()
        self.conn.execute(
            "INSERT INTO params_versions(project_id,version,parent_version,params_json,"
            "fingerprint,committed_at,author,note) VALUES (?,?,?,?,?,?,?,?)",
            (project_id, version, parent_version,
             json.dumps(params, ensure_ascii=False, sort_keys=True), fp, at, actor, note))
        self.conn.commit()
        payload = {"version": version, "parent_version": parent_version,
                   "fingerprint": fp, "note": note, "params": params}
        event = self.add_event(project_id, "params_revised", payload, actor,
                               parent_event, branch)
        return {"version": version, "fingerprint": fp, "event": event}

    def get_params(self, project_id: str, version: int) -> Dict[str, Any]:
        if version == 0:
            from .assign import DEFAULT_PARAMS
            return DEFAULT_PARAMS
        row = self.conn.execute(
            "SELECT params_json FROM params_versions WHERE project_id=? AND version=?",
            (project_id, version)).fetchone()
        return json.loads(row["params_json"])

    # ---------- 派生结果 ----------
    def save_result(self, project_id: str, result: Any, params_version: int,
                    event_id_: str) -> None:
        payload = json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True)
        existing = self.conn.execute("SELECT id FROM results WHERE id=?",
                                     (result.id,)).fetchone()
        if existing:
            return
        at = self.clock.now()
        self.conn.execute(
            "INSERT INTO results(id,project_id,nucleus,algo_version,fingerprint,"
            "conditions_signature,params_version,created_at,payload_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (result.id, project_id, result.nucleus, result.algo_version,
             result.param_fingerprint, result.conditions_signature, params_version,
             at, payload))
        for role, ids in (("peaks", result.input_record_ids.get("peaks", [])),
                          ("edges", result.input_record_ids.get("edges", [])),
                          ("sites", result.input_record_ids.get("sites", []))):
            for rid in ids:
                self.conn.execute(
                    "INSERT OR IGNORE INTO result_inputs(result_id,record_id,role)"
                    " VALUES (?,?,?)", (result.id, rid, role))
        self.conn.commit()

    def all_results(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM results WHERE project_id=?",
                                 (project_id,)).fetchall()
        return [{"id": r["id"], "nucleus": r["nucleus"],
                 "algo_version": r["algo_version"], "fingerprint": r["fingerprint"],
                 "conditions_signature": r["conditions_signature"],
                 "params_version": r["params_version"], "created_at": r["created_at"],
                 "payload": json.loads(r["payload_json"])} for r in rows]

    def result_deps(self, result_id: str) -> List[Tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT record_id,role FROM result_inputs WHERE result_id=?",
            (result_id,)).fetchall()
        return [(r["record_id"], r["role"]) for r in rows]

    def record_exists(self, record_id: str, docv: Dict[str, int]) -> bool:
        rows = self.conn.execute(
            "SELECT doc_id,version FROM records WHERE id=?", (record_id,)).fetchall()
        return any((r["doc_id"] in docv and docv[r["doc_id"]] == r["version"]) for r in rows)

    def current_record_versions(self, project_id: str, docv: Dict[str, int]
                                ) -> Dict[str, Tuple[str, int]]:
        out: Dict[str, Tuple[str, int]] = {}
        for d_id, version in docv.items():
            for r in self.conn.execute("SELECT id FROM records WHERE doc_id=? AND version=?",
                                       (d_id, version)).fetchall():
                out[r["id"]] = (d_id, version)
        return out

    # ---------- 人工决定 / 冲突 / 撤销 ----------
    def add_decision_event(self, project_id: str, site_id: str, peak_id: Optional[str],
                           result_id: str, verdict: str, actor: str, rationale: str,
                           parent_event: str, branch: str,
                           supersedes: Optional[str] = None) -> Dict[str, Any]:
        payload = {"site_id": site_id, "peak_id": peak_id, "result_id": result_id,
                   "verdict": verdict, "rationale": rationale,
                   "supersedes_decision": supersedes}
        event = self.add_event(project_id, "decision_made", payload, actor,
                               parent_event, branch)
        did = stable_id("dec", event["id"])
        self.conn.execute(
            "INSERT INTO decisions(id,project_id,event_id,site_id,peak_id,result_id,"
            "verdict,actor,rationale,supersedes_decision,adopted,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
            (did, project_id, event["id"], site_id, peak_id, result_id, verdict,
             actor, rationale, supersedes, event["at"]))
        self.conn.commit()
        return {"decision_id": did, "event": event}

    def open_conflict(self, project_id: str, site_id: str, decision_a: str,
                      decision_b: str, event_id_: str) -> str:
        cid = stable_id("conf", project_id, site_id, decision_a, decision_b)
        existing = self.conn.execute("SELECT id FROM conflicts WHERE id=?", (cid,)).fetchone()
        if not existing:
            at = self.clock.now()
            self.conn.execute(
                "INSERT INTO conflicts(id,project_id,site_id,status,created_at)"
                " VALUES (?,?,?,'open',?)", (cid, project_id, site_id, at))
        for did in (decision_a, decision_b):
            self.conn.execute("UPDATE decisions SET conflict_id=? WHERE id=?", (cid, did))
        self.conn.commit()
        return cid

    def resolve_conflict(self, project_id: str, conflict_id: str, winner_decision: str,
                         loser_decision: str, actor: str, rationale: str,
                         parent_event: str, branch: str) -> Dict[str, Any]:
        event = self.add_event(project_id, "conflict_resolved",
                               {"conflict_id": conflict_id,
                                "winner_decision": winner_decision,
                                "loser_decision": loser_decision,
                                "rationale": rationale}, actor, parent_event, branch)
        self.conn.execute("UPDATE conflicts SET status='resolved',resolution_event_id=? WHERE id=?",
                          (event["id"], conflict_id))
        self.conn.execute("UPDATE decisions SET adopted=0 WHERE id=?", (loser_decision,))
        self.conn.execute("UPDATE decisions SET adopted=1 WHERE id=?", (winner_decision,))
        self.conn.commit()
        return event

    def undo_decision(self, project_id: str, decision_event_id: str, actor: str,
                      parent_event: str, branch: str) -> Dict[str, Any]:
        event = self.add_event(project_id, "decision_undone",
                               {"undone_event": decision_event_id}, actor,
                               parent_event, branch)
        self.conn.execute("UPDATE events SET undone=1,undone_by=? WHERE id=?",
                          (event["id"], decision_event_id))
        self.conn.execute(
            "UPDATE decisions SET adopted=0 WHERE event_id=?", (decision_event_id,))
        self.conn.commit()
        return event

    def conditions_change_event(self, project_id: str, doc_id: str, change_kind: str,
                                old_sig: str, new_sig: str, affected_nucleus: str,
                                actor: str, parent_event: str, branch: str) -> Dict[str, Any]:
        return self.add_event(project_id, "conditions_changed",
                              {"doc_id": doc_id, "change_kind": change_kind,
                               "old_conditions_signature": old_sig,
                               "new_conditions_signature": new_sig,
                               "affected_nucleus": affected_nucleus},
                              actor, parent_event, branch)

    def add_scenario_finding(self, project_id: str, code: str, scope: str,
                             payload: Dict[str, Any], event_id_: Optional[str]) -> str:
        fid = stable_id("find", project_id, code, scope, digest([payload]))
        self.conn.execute(
            "INSERT OR IGNORE INTO scenario_findings(id,project_id,event_id,code,scope,"
            "payload_json,at) VALUES (?,?,?,?,?,?,?)",
            (fid, project_id, event_id_, code, scope,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), self.clock.now()))
        self.conn.commit()
        return fid

    def list_scenario_findings(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM scenario_findings WHERE project_id=? ORDER BY at,code",
            (project_id,)).fetchall()
        return [{"id": r["id"], "event_id": r["event_id"], "code": r["code"],
                 "scope": r["scope"], "payload": json.loads(r["payload_json"]),
                 "at": r["at"]} for r in rows]

    def list_decisions(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM decisions WHERE project_id=?",
                                 (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_conflicts(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM conflicts WHERE project_id=?",
                                 (project_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 导出 / 再导入 ----------
    def export_bundle(self, project_id: str) -> Dict[str, Any]:
        tables: Dict[str, List[Dict[str, Any]]] = {}
        direct = {"projects", "documents", "document_versions", "params_versions",
                  "results", "events", "decisions", "conflicts", "scenario_findings"}
        for name in ("projects", "documents", "document_versions", "records",
                     "params_versions", "results", "result_inputs", "events",
                     "decisions", "conflicts", "scenario_findings"):
            if name == "result_inputs":
                rows = self.conn.execute(
                    "SELECT ri.* FROM result_inputs ri JOIN results r"
                    " ON r.id=ri.result_id WHERE r.project_id=?",
                    (project_id,)).fetchall()
            elif name in direct:
                col = "id" if name == "projects" else "project_id"
                rows = self.conn.execute(
                    f"SELECT t.* FROM {name} t WHERE t.{col}=?",
                    (project_id,)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT r.* FROM records r JOIN document_versions dv"
                    " ON dv.doc_id=r.doc_id AND dv.version=r.version"
                    " WHERE dv.project_id=?", (project_id,)).fetchall()
            tables[name] = [dict(r) for r in rows]
        return {"bundle_format": "NMRB-BUNDLE/1", "project_id": project_id,
                "tables": tables,
                "bundle_sha": digest([{k: len(v) for k, v in tables.items()}])}

    def import_bundle(self, bundle: Dict[str, Any]) -> str:
        if bundle.get("bundle_format") != "NMRB-BUNDLE/1":
            raise ValueError("不支持的归档格式")
        pid = bundle["project_id"]
        tables = bundle["tables"]
        for name in ("projects", "documents", "document_versions", "records",
                     "params_versions", "results", "result_inputs", "events",
                     "decisions", "conflicts", "scenario_findings"):
            for row in tables.get(name, []):
                cols = list(row.keys())
                placeholders = ",".join("?" for _ in cols)
                self.conn.execute(
                    f"INSERT OR REPLACE INTO {name}({','.join(cols)})"
                    f" VALUES ({placeholders})", [row[c] for c in cols])
        self.conn.commit()
        return pid
