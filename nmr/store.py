from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    branch TEXT NOT NULL,
    parent_event_id TEXT,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (project_id, seq)
);
CREATE TABLE IF NOT EXISTS event_ancestors (
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    ancestor_id TEXT NOT NULL,
    depth INTEGER NOT NULL,
    PRIMARY KEY (event_id, ancestor_id)
);
CREATE TABLE IF NOT EXISTS branches (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    branch TEXT NOT NULL,
    parent_branch TEXT,
    head_event_id TEXT NOT NULL,
    PRIMARY KEY (project_id, branch)
);
CREATE TABLE IF NOT EXISTS source_files (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    file_version TEXT NOT NULL,
    parent_version TEXT,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    PRIMARY KEY (project_id, source_id, file_version)
);
CREATE TABLE IF NOT EXISTS runs (
    project_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    state_event_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (project_id, branch, state_event_id)
);
"""

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("NMR_DB_PATH", "nmr_workbench.sqlite3")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()

@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise

def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def loads(text: str) -> Any:
    return json.loads(text)

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def make_event_id(project_id: str, seq: int, event_type: str, payload: Dict[str, Any]) -> str:
    raw = f"{project_id}|{seq}|{event_type}|{dumps(payload)}"
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def create_project(conn: sqlite3.Connection, project_id: str, name: str, created_at: Optional[str] = None) -> str:
    created_at = created_at or utc_now()
    with transaction(conn):
        conn.execute("INSERT INTO projects(project_id,name,created_at) VALUES(?,?,?)", (project_id, name, created_at))
        payload = {"project_id": project_id, "name": name}
        event_id = append_event(conn, project_id=project_id, branch="main", parent_event_id=None,
                                event_type="project_created", actor="system", payload=payload,
                                created_at=created_at, create_branch=True)
    return event_id

def append_event(conn: sqlite3.Connection, *, project_id: str, branch: str, parent_event_id: Optional[str],
                 event_type: str, actor: str, payload: Dict[str, Any], created_at: Optional[str] = None,
                 create_branch: bool = False, supplied_event_id: Optional[str] = None) -> str:
    created_at = created_at or utc_now()
    seq = int(conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM events WHERE project_id=?", (project_id,)).fetchone()["s"]) + 1
    event_id = supplied_event_id or make_event_id(project_id, seq, event_type, payload)
    conn.execute("""INSERT INTO events(project_id,seq,event_id,branch,parent_event_id,type,actor,created_at,payload_json)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                 (project_id, seq, event_id, branch, parent_event_id, event_type, actor, created_at, dumps(payload)))
    conn.execute("INSERT INTO event_ancestors(event_id,ancestor_id,depth) VALUES(?,?,0)", (event_id, event_id))
    if parent_event_id:
        conn.execute("""INSERT INTO event_ancestors(event_id,ancestor_id,depth)
                        SELECT ?, ancestor_id, depth + 1 FROM event_ancestors WHERE event_id=?""",
                     (event_id, parent_event_id))
    if create_branch:
        conn.execute("""INSERT INTO branches(project_id,branch,parent_branch,head_event_id)
                        VALUES(?,?,?,?) ON CONFLICT(project_id,branch)
                        DO UPDATE SET head_event_id=excluded.head_event_id""",
                     (project_id, branch, payload.get("parent_branch"), event_id))
    else:
        if not conn.execute("SELECT 1 FROM branches WHERE project_id=? AND branch=?", (project_id, branch)).fetchone():
            conn.execute("INSERT INTO branches(project_id,branch,parent_branch,head_event_id) VALUES(?,?,?,?)",
                         (project_id, branch, None, event_id))
        conn.execute("UPDATE branches SET head_event_id=? WHERE project_id=? AND branch=?",
                     (event_id, project_id, branch))
    return event_id

def get_branch_head(conn: sqlite3.Connection, project_id: str, branch: str) -> Optional[str]:
    row = conn.execute("SELECT head_event_id FROM branches WHERE project_id=? AND branch=?", (project_id, branch)).fetchone()
    return row["head_event_id"] if row else None

def branch_events(conn: sqlite3.Connection, project_id: str, branch: str, at_event_id: Optional[str] = None):
    head = at_event_id or get_branch_head(conn, project_id, branch)
    if not head:
        return []
    return list(conn.execute("""SELECT e.* FROM events e JOIN event_ancestors a ON a.ancestor_id=e.event_id
                                WHERE a.event_id=? AND e.project_id=? ORDER BY e.seq""", (head, project_id)).fetchall())

def list_branches(conn: sqlite3.Connection, project_id: str):
    return list(conn.execute("SELECT * FROM branches WHERE project_id=? ORDER BY branch", (project_id,)).fetchall())

def insert_source_file(conn: sqlite3.Connection, *, project_id: str, source_id: str, file_version: str,
                       parent_version: Optional[str], filename: str, media_type: str, raw_text: str) -> str:
    digest = sha256_text(raw_text)
    conn.execute("""INSERT INTO source_files(project_id,source_id,file_version,parent_version,filename,media_type,content_sha256,raw_text)
                    VALUES(?,?,?,?,?,?,?,?)""",
                 (project_id, source_id, file_version, parent_version, filename, media_type, digest, raw_text))
    return digest

def get_source_file(conn: sqlite3.Connection, project_id: str, source_id: str, version: str):
    return conn.execute("SELECT * FROM source_files WHERE project_id=? AND source_id=? AND file_version=?",
                        (project_id, source_id, version)).fetchone()

def list_projects(conn: sqlite3.Connection):
    return list(conn.execute("SELECT * FROM projects ORDER BY project_id").fetchall())

def export_project(conn: sqlite3.Connection, project_id: str) -> Dict[str, Any]:
    project = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if not project:
        raise KeyError(project_id)
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,))]
    ancestors = [dict(r) for r in conn.execute("""SELECT a.* FROM event_ancestors a JOIN events e ON e.event_id=a.event_id
                                                  WHERE e.project_id=?""", (project_id,))]
    files = [dict(r) for r in conn.execute("SELECT * FROM source_files WHERE project_id=?", (project_id,))]
    branches = [dict(r) for r in conn.execute("SELECT * FROM branches WHERE project_id=?", (project_id,))]
    return {"format": "nmr-candidate-network-archive/1", "project": dict(project), "events": events,
            "event_ancestors": ancestors, "branches": branches, "source_files": files}

def import_project(conn: sqlite3.Connection, archive: Dict[str, Any], overwrite: bool = False) -> str:
    if archive.get("format") != "nmr-candidate-network-archive/1":
        raise ValueError("unsupported archive format")
    project = archive["project"]
    project_id = project["project_id"]
    with transaction(conn):
        if overwrite:
            conn.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
        elif conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
            raise ValueError(f"project {project_id} already exists; use overwrite=true")
        conn.execute("INSERT INTO projects(project_id,name,created_at) VALUES(?,?,?)",
                     (project_id, project["name"], project["created_at"]))
        for row in archive.get("source_files", []):
            conn.execute("""INSERT INTO source_files(project_id,source_id,file_version,parent_version,filename,media_type,content_sha256,raw_text)
                            VALUES(?,?,?,?,?,?,?,?)""",
                         tuple(row[k] for k in ("project_id","source_id","file_version","parent_version","filename","media_type","content_sha256","raw_text")))
        for event in archive["events"]:
            conn.execute("""INSERT INTO events(project_id,seq,event_id,branch,parent_event_id,type,actor,created_at,payload_json)
                            VALUES(?,?,?,?,?,?,?,?,?)""",
                         tuple(event[k] for k in ("project_id","seq","event_id","branch","parent_event_id","type","actor","created_at","payload_json")))
        for ancestor in archive.get("event_ancestors", []):
            conn.execute("INSERT INTO event_ancestors(event_id,ancestor_id,depth) VALUES(?,?,?)",
                         (ancestor["event_id"], ancestor["ancestor_id"], ancestor["depth"]))
        for branch in archive["branches"]:
            conn.execute("INSERT INTO branches(project_id,branch,parent_branch,head_event_id) VALUES(?,?,?,?)",
                         tuple(branch[k] for k in ("project_id","branch","parent_branch","head_event_id")))
    return project_id

def save_run(conn: sqlite3.Connection, project_id: str, branch: str, state_event_id: str, run_id: str,
             result: Dict[str, Any], status: str = "current") -> None:
    conn.execute("""INSERT OR REPLACE INTO runs(project_id,branch,state_event_id,run_id,status,result_json)
                    VALUES(?,?,?,?,?,?)""", (project_id, branch, state_event_id, run_id, status, dumps(result)))
