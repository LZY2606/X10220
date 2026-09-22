"""SQLite persistence with stable identities and preserved ordering.

The database is a local, append-only record of projects, immutable source
documents, replayable event nodes and cached builds.  Deleting it and
re-importing a bundle restores ids, order and provenance exactly.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    seed INTEGER NOT NULL DEFAULT 1191
);
CREATE TABLE IF NOT EXISTS sources (
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    role TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_source_id TEXT,
    raw_text TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    ord INTEGER NOT NULL,
    PRIMARY KEY (project_id, source_id)
);
CREATE TABLE IF NOT EXISTS nodes (
    project_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    parent_node_id TEXT,
    branch TEXT NOT NULL,
    ord INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, node_id)
);
CREATE TABLE IF NOT EXISTS builds (
    project_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    build_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    rulepack_fingerprint TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, node_id)
);
CREATE TABLE IF NOT EXISTS kvmeta (
    project_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:" and os.path.dirname(db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _rowdict(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


class Storage:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- projects ----
    def create_project(self, project_id: str, title: str, now: str,
                       seed: int = 1191) -> None:
        self.conn.execute(
            "INSERT INTO projects(project_id,title,created_at,updated_at,seed)"
            " VALUES (?,?,?,?,?)",
            (project_id, title, now, now, seed))
        self.conn.commit()

    def touch_project(self, project_id: str, now: str) -> None:
        self.conn.execute(
            "UPDATE projects SET updated_at=? WHERE project_id=?",
            (now, project_id))
        self.conn.commit()

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        return _rowdict(row) if row else None

    def list_projects(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM projects ORDER BY created_at, project_id").fetchall()
        return [_rowdict(r) for r in rows]

    # ---- immutable sources ----
    def add_source(self, project_id: str, source: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO sources(project_id,source_id,role,doc_type,filename,"
            "version,parent_source_id,raw_text,raw_sha256,parsed_json,"
            "submitted_by,submitted_at,ord) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, source["source_id"], source["role"],
             source["doc_type"], source["filename"], source["version"],
             source.get("parent_source_id"), source["raw_text"],
             source["raw_sha256"], json.dumps(source["parsed"], sort_keys=True),
             source["submitted_by"], source["submitted_at"], source["ord"]))
        self.conn.commit()

    def list_sources(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE project_id=? ORDER BY ord, source_id",
            (project_id,)).fetchall()
        return [_rowdict(r) for r in rows]

    def get_source(self, project_id: str, source_id: str
                   ) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE project_id=? AND source_id=?",
            (project_id, source_id)).fetchone()
        return _rowdict(row) if row else None

    def source_exists(self, source_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sources WHERE source_id=? LIMIT 1",
            (source_id,)).fetchone()
        return row is not None

    # ---- event nodes ----
    def add_node(self, project_id: str, node: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO nodes(project_id,node_id,parent_node_id,branch,ord,"
            "seq,kind,label,event_json,created_by,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, node["node_id"], node.get("parent_node_id"),
             node["branch"], node["ord"], node["seq"], node["kind"],
             node["label"], json.dumps(node["event"], sort_keys=True),
             node["created_by"], node["created_at"]))
        self.conn.commit()

    def list_nodes(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE project_id=? ORDER BY ord, node_id",
            (project_id,)).fetchall()
        out = []
        for r in rows:
            d = _rowdict(r)
            d["event"] = json.loads(d.pop("event_json"))
            out.append(d)
        return out

    # ---- builds ----
    def put_build(self, project_id: str, node_id: str, build: Dict[str, Any],
                  input_fingerprint: str, stale: bool = False) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO builds(project_id,node_id,build_json,"
            "input_fingerprint,engine_version,rulepack_fingerprint,stale)"
            " VALUES (?,?,?,?,?,?,?)",
            (project_id, node_id, json.dumps(build, sort_keys=True),
             input_fingerprint, build["engine_version"],
             build["rulepack_fingerprint"], 1 if stale else 0))
        self.conn.commit()

    def get_build(self, project_id: str, node_id: str
                  ) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM builds WHERE project_id=? AND node_id=?",
            (project_id, node_id)).fetchone()
        if not row:
            return None
        d = _rowdict(row)
        d["build"] = json.loads(d.pop("build_json"))
        d["stale"] = bool(d["stale"])
        return d

    # ---- meta ----
    def set_meta(self, project_id: str, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kvmeta(project_id,key,value) VALUES (?,?,?)",
            (project_id, key, json.dumps(value, sort_keys=True)))
        self.conn.commit()

    def get_meta(self, project_id: str, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM kvmeta WHERE project_id=? AND key=?",
            (project_id, key)).fetchone()
        return json.loads(row["value"]) if row else default

    # ---- bundle ----
    def export_project(self, project_id: str) -> Dict[str, Any]:
        project = self.get_project(project_id)
        sources = []
        for row in self.list_sources(project_id):
            row = dict(row)
            row["parsed"] = json.loads(row.pop("parsed_json"))
            sources.append(row)
        nodes = []
        for row in self.list_nodes(project_id):
            nodes.append(row)
        builds = []
        rows = self.conn.execute(
            "SELECT * FROM builds WHERE project_id=?", (project_id,)).fetchall()
        for r in rows:
            d = _rowdict(r)
            d["build"] = json.loads(d.pop("build_json"))
            d["stale"] = bool(d["stale"])
            builds.append(d)
        kvrows = self.conn.execute(
            "SELECT key,value FROM kvmeta WHERE project_id=? ORDER BY key",
            (project_id,)).fetchall()
        meta = {r["key"]: json.loads(r["value"]) for r in kvrows}
        return {"format": "nmrwb-bundle/1", "project": project,
                "sources": sources, "nodes": nodes, "builds": builds,
                "meta": meta}

    def import_project(self, bundle: Dict[str, Any]) -> str:
        project = bundle["project"]
        pid = project["project_id"]
        if self.get_project(pid):
            raise ValueError("project %s already exists" % pid)
        self.create_project(pid, project["title"], project["created_at"],
                            project.get("seed", 1191))
        self.conn.execute("UPDATE projects SET updated_at=? WHERE project_id=?",
                          (project["updated_at"], pid))
        for source in bundle["sources"]:
            if isinstance(source.get("parsed"), str):
                source = dict(source)
                source["parsed"] = json.loads(source["parsed"])
            self.add_source(pid, source)
        for node in bundle["nodes"]:
            self.add_node(pid, node)
        for build in bundle["builds"]:
            self.conn.execute(
                "INSERT OR REPLACE INTO builds(project_id,node_id,build_json,"
                "input_fingerprint,engine_version,rulepack_fingerprint,stale)"
                " VALUES (?,?,?,?,?,?,?)",
                (pid, build["node_id"],
                 json.dumps(build["build"], sort_keys=True),
                 build["input_fingerprint"], build["engine_version"],
                 build["rulepack_fingerprint"], 1 if build.get("stale") else 0))
        for key, value in bundle.get("meta", {}).items():
            self.set_meta(pid, key, value)
        self.conn.commit()
        return pid
