"""Append-only event store on SQLite.

Everything the workbench knows is derived by replaying events. Raw inputs
are immutable once submitted; revisions are new versions linked by
parent_version. Every derived value records algo version, parameter
fingerprint and its direct sources, so any historical event can be
replayed to recover the dependency versions and visible conclusions of
that moment.
"""
from __future__ import annotations

import json
import sqlite3

from .clock import utc_now_iso
from .engine import PARAM_DEFAULTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    skeleton TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    branch_id TEXT NOT NULL,
    parent_event_id TEXT,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inputs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    raw_text TEXT NOT NULL,
    parsed TEXT NOT NULL,
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS derived (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    input_id TEXT NOT NULL,
    atom_id TEXT NOT NULL,
    algo_version TEXT NOT NULL,
    param_fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path, clock=None):
        self.db_path = str(db_path)
        self.clock = clock or utc_now_iso
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------------- projects ----------------
    def create_project(self, name, skeleton, project_id=None):
        pid = project_id or "prj-%s" % abs(hash((name, self.clock()))) % 10**8
        self.conn.execute(
            "INSERT INTO projects(id,name,skeleton,created_at) VALUES(?,?,?,?)",
            (pid, name, json.dumps(skeleton), self.clock()))
        self.conn.commit()
        return pid

    def get_project(self, project_id):
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "name": row["name"],
                "skeleton": json.loads(row["skeleton"]), "created_at": row["created_at"]}

    def list_projects(self):
        rows = self.conn.execute("SELECT * FROM projects ORDER BY created_at, id").fetchall()
        return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"]} for r in rows]

    # ---------------- events ----------------
    def add_event(self, project_id, type_, payload, actor="system",
                  branch_id=None, parent_event_id="AUTO"):
        if parent_event_id == "AUTO":
            head = self.conn.execute(
                "SELECT id, branch_id FROM events WHERE project_id=? ORDER BY seq DESC LIMIT 1",
                (project_id,)).fetchone()
            parent_event_id = head["id"] if head else None
            inherited_branch = head["branch_id"] if head else None
        else:
            head = self.conn.execute("SELECT branch_id FROM events WHERE id=?",
                                     (parent_event_id,)).fetchone()
            inherited_branch = head["branch_id"] if head else None
        seq_row = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS s FROM events WHERE project_id=?",
            (project_id,)).fetchone()
        seq = seq_row["s"]
        eid = "%s:ev-%06d" % (project_id, seq)
        branch_id = branch_id or inherited_branch or "br-main"
        self.conn.execute(
            "INSERT INTO events(id,project_id,seq,branch_id,parent_event_id,type,actor,payload,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (eid, project_id, seq, branch_id, parent_event_id, type_, actor,
             json.dumps(payload), self.clock()))
        self.conn.commit()
        return {"id": eid, "seq": seq, "branch_id": branch_id,
                "parent_event_id": parent_event_id, "type": type_, "actor": actor}

    def events(self, project_id):
        rows = self.conn.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()
        return [self._event_dict(r) for r in rows]

    @staticmethod
    def _event_dict(r):
        return {"id": r["id"], "project_id": r["project_id"], "seq": r["seq"],
                "branch_id": r["branch_id"], "parent_event_id": r["parent_event_id"],
                "type": r["type"], "actor": r["actor"],
                "payload": json.loads(r["payload"]), "created_at": r["created_at"]}

    # ---------------- inputs ----------------
    def add_input(self, project_id, kind, raw_text, parsed, event_id):
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM inputs WHERE project_id=? AND kind=?",
            (project_id, kind)).fetchone()
        version = row["v"]
        iid = "%s:in-%s-%03d" % (project_id, kind, version)
        self.conn.execute(
            "INSERT INTO inputs(id,project_id,kind,version,parent_version,raw_text,parsed,event_id,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (iid, project_id, kind, version, version - 1 or None,
             raw_text, json.dumps(parsed), event_id, self.clock()))
        self.conn.commit()
        return {"id": iid, "version": version, "parent_version": version - 1 or None}

    def get_input(self, input_id):
        r = self.conn.execute("SELECT * FROM inputs WHERE id=?", (input_id,)).fetchone()
        if not r:
            return None
        return {"id": r["id"], "project_id": r["project_id"], "kind": r["kind"],
                "version": r["version"], "parent_version": r["parent_version"],
                "raw_text": r["raw_text"], "parsed": json.loads(r["parsed"]),
                "event_id": r["event_id"], "created_at": r["created_at"]}

    def inputs(self, project_id):
        rows = self.conn.execute(
            "SELECT * FROM inputs WHERE project_id=? ORDER BY kind, version", (project_id,)).fetchall()
        return [self.get_input(r["id"]) for r in rows]

    # ---------------- derived ----------------
    def add_derived(self, project_id, input_id, atom_id, algo_version,
                    param_fingerprint, payload, event_id):
        did = "%s:der-%s-%s-%s" % (project_id, input_id.rsplit(":", 1)[-1],
                                   atom_id, param_fingerprint[:8])
        self.conn.execute(
            "INSERT OR REPLACE INTO derived(id,project_id,input_id,atom_id,algo_version,"
            "param_fingerprint,payload,event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (did, project_id, input_id, atom_id, algo_version, param_fingerprint,
             json.dumps(payload), event_id, self.clock()))
        self.conn.commit()
        return did

    def derived_rows(self, project_id):
        rows = self.conn.execute(
            "SELECT * FROM derived WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [{"id": r["id"], "input_id": r["input_id"], "atom_id": r["atom_id"],
                 "algo_version": r["algo_version"],
                 "param_fingerprint": r["param_fingerprint"],
                 "payload": json.loads(r["payload"]), "event_id": r["event_id"],
                 "created_at": r["created_at"]} for r in rows]

    # ---------------- replay ----------------
    def replay(self, project_id, at_event_id=None):
        """Rebuild visible state as of `at_event_id` (default: head).

        Replaying an old event restores the dependency versions and visible
        conclusions of that moment; later computations never overwrite
        history.
        """
        all_events = self.events(project_id)
        by_id = {e["id"]: e for e in all_events}
        if at_event_id is None:
            at_event_id = all_events[-1]["id"] if all_events else None
        if at_event_id is None:
            visible = []
        else:
            if at_event_id not in by_id:
                raise KeyError("unknown event %s" % at_event_id)
            visible = []
            cur = at_event_id
            while cur is not None:
                visible.append(by_id[cur])
                cur = by_id[cur]["parent_event_id"]
            visible.reverse()

        undone = {e["payload"]["target_event_id"] for e in visible
                  if e["type"] == "undo" and "target_event_id" in e["payload"]}

        state = {
            "project_id": project_id,
            "viewing_event": at_event_id,
            "params": dict(PARAM_DEFAULTS),
            "inputs": {},
            "derived": {},
            "decisions": {},
            "conflicts": {},
            "guards": [],
            "branches": {},
            "undone_event_ids": sorted(undone),
            "events": [],
        }
        for e in visible:
            state["events"].append({"id": e["id"], "seq": e["seq"], "type": e["type"],
                                    "actor": e["actor"], "branch_id": e["branch_id"],
                                    "created_at": e["created_at"],
                                    "undone": e["id"] in undone,
                                    "payload": e["payload"]})
            state["branches"].setdefault(e["branch_id"], []).append(e["id"])
            if e["id"] in undone:
                continue
            self._apply(state, e)
        return state

    def _apply(self, state, e):
        t = e["type"]
        p = e["payload"]
        if t == "import_input":
            inp = self.get_input(p["input_id"])
            if inp:
                state["inputs"][inp["id"]] = {
                    "id": inp["id"], "kind": inp["kind"], "version": inp["version"],
                    "parent_version": inp["parent_version"], "meta": inp["parsed"]["meta"],
                    "diagnostics": inp["parsed"]["diagnostics"],
                    "parse_version": inp["parsed"]["parse_version"],
                    "event_id": e["id"],
                }
        elif t == "revise_parameters":
            state["params"].update(p["changes"])
        elif t == "compute":
            for did, record in p["derived"].items():
                state["derived"][did] = dict(record, status="current", stale_reason=None)
            state["guards"] = p.get("guards", [])
        elif t == "invalidate":
            for did in p["affected_derived_ids"]:
                if did in state["derived"]:
                    state["derived"][did]["status"] = "stale"
                    state["derived"][did]["stale_reason"] = p.get("reason")
        elif t == "decide":
            atom = p["atom_id"]
            existing = state["decisions"].get(atom)
            if existing and existing["actor"] != e["actor"] and not existing.get("resolved") \
                    and (existing["candidate_id"] != p.get("candidate_id")
                         or existing["action"] != p["action"]):
                cid = "%s:conflict-%s" % (state["project_id"], e["id"].rsplit("-", 1)[-1])
                state["conflicts"][cid] = {
                    "id": cid, "atom_id": atom, "status": "open",
                    "parties": [existing, {
                        "actor": e["actor"], "action": p["action"],
                        "candidate_id": p.get("candidate_id"), "event_id": e["id"],
                    }],
                    "created_by_event": e["id"],
                }
                existing["contested"] = True
            state["decisions"][atom] = {
                "actor": e["actor"], "action": p["action"],
                "candidate_id": p.get("candidate_id"), "note": p.get("note"),
                "event_id": e["id"], "branch_id": e["branch_id"],
                "created_at": e["created_at"],
            }
        elif t == "resolve_conflict":
            cid = p["conflict_id"]
            if cid in state["conflicts"]:
                conf = state["conflicts"][cid]
                conf["status"] = "resolved"
                conf["resolved_by"] = e["actor"]
                conf["resolved_event_id"] = e["id"]
                conf["adopted_event_id"] = p["adopted_event_id"]
                for party in conf["parties"]:
                    party["adopted"] = party["event_id"] == p["adopted_event_id"]
                atom = conf["atom_id"]
                adopted = next((x for x in conf["parties"] if x["adopted"]), None)
                if adopted:
                    state["decisions"][atom] = dict(
                        adopted, resolved=True,
                        note="adopted by conflict resolution %s" % e["id"])

    # ---------------- export / import ----------------
    def export_project(self, project_id):
        project = self.get_project(project_id)
        if not project:
            raise KeyError("unknown project %s" % project_id)
        return {
            "format": "nmrwb-export/1",
            "project": project,
            "events": self.events(project_id),
            "inputs": self.inputs(project_id),
            "derived": self.derived_rows(project_id),
        }

    def import_project(self, data):
        """Import an export document into this (possibly empty) database.

        Identities, ordering and lineage are preserved verbatim.
        """
        if data.get("format") != "nmrwb-export/1":
            raise ValueError("unsupported export format")
        project = data["project"]
        if self.get_project(project["id"]):
            raise ValueError("project %s already exists" % project["id"])
        self.conn.execute(
            "INSERT INTO projects(id,name,skeleton,created_at) VALUES(?,?,?,?)",
            (project["id"], project["name"], json.dumps(project["skeleton"]),
             project["created_at"]))
        for e in data["events"]:
            self.conn.execute(
                "INSERT INTO events(id,project_id,seq,branch_id,parent_event_id,type,actor,payload,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (e["id"], e["project_id"], e["seq"], e["branch_id"],
                 e["parent_event_id"], e["type"], e["actor"],
                 json.dumps(e["payload"]), e["created_at"]))
        for i in data["inputs"]:
            self.conn.execute(
                "INSERT INTO inputs(id,project_id,kind,version,parent_version,raw_text,parsed,event_id,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (i["id"], i["project_id"], i["kind"], i["version"], i["parent_version"],
                 i["raw_text"], json.dumps(i["parsed"]), i["event_id"], i["created_at"]))
        for d in data["derived"]:
            self.conn.execute(
                "INSERT INTO derived(id,project_id,input_id,atom_id,algo_version,"
                "param_fingerprint,payload,event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (d["id"], project["id"], d["input_id"], d["atom_id"], d["algo_version"],
                 d["param_fingerprint"], json.dumps(d["payload"]), d["event_id"],
                 d["created_at"]))
        self.conn.commit()
        return project["id"]
