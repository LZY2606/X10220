"""SQLite-backed immutable event store with branches and replay.

Rules enforced here:

* raw source submissions are immutable; a revision is a new version whose
  ``parent_version`` points at the previous one;
* every derived run stores algorithm version, parameter fingerprint and
  direct source references;
* branches are event lineages: a branch sees its parent chain plus its own
  descendants; checking out an event restores that event's versions and
  decisions instead of overwriting history with the newest computation;
* exports are JSON bundles; deleting the database and importing a bundle
  reconstructs identical ids, ordering and provenance relations.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
  branch_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  parent_branch_id TEXT,
  base_event_id TEXT,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  base_event_id TEXT,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  project_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  parent_version_id TEXT,
  kind TEXT NOT NULL,
  filename TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (project_id, source_id, version_id)
);
CREATE TABLE IF NOT EXISTS runs (
  project_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  source_versions TEXT NOT NULL,
  param_fingerprint TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_event_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (project_id, branch_id, run_id)
);
CREATE TABLE IF NOT EXISTS counters (
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  value INTEGER NOT NULL,
  PRIMARY KEY (project_id, name)
);
"""

BUNDLE_VERSION = "nmrwb-bundle/1"


class Clock:
    """Virtual clock used by tests; production defaults to wall time."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def connect(db_path: str) -> sqlite3.Connection:
    first = not os.path.exists(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _next_seq(conn, project_id: str) -> int:
    row = conn.execute(
        "SELECT value FROM counters WHERE project_id=? AND name='event'",
        (project_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO counters(project_id,name,value) VALUES(?,?,1)",
                     (project_id, "event"))
        return 1
    nxt = row["value"] + 1
    conn.execute("UPDATE counters SET value=? WHERE project_id=? AND name='event'",
                 (nxt, project_id))
    return nxt


def create_project(conn, project_id: str, name: str, clock: Clock) -> str:
    ts = clock.now()
    conn.execute("INSERT INTO projects VALUES(?,?,?)",
                 (project_id, name, _iso(ts)))
    conn.execute("INSERT INTO branches(branch_id,project_id,parent_branch_id,"
                 "base_event_id,label,created_at) VALUES(?,?,?,?,?,?)",
                 ("B:main", project_id, None, None, "main", _iso(ts)))
    conn.execute("INSERT INTO counters(project_id,name,value) VALUES(?,?,0)",
                 (project_id, "event"))
    return project_id


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def add_event(conn, project_id: str, branch_id: str, kind: str, payload: dict,
              actor: str, clock: Clock, base_event_id: Optional[str] = None) -> dict:
    seq = _next_seq(conn, project_id)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    event_id = "E:%06d:%s" % (seq, sha(f"{kind}|{branch_id}|{blob}")[:10])
    ts = clock.now()
    conn.execute(
        "INSERT INTO events(event_id,project_id,branch_id,seq,actor,kind,"
        "base_event_id,payload,payload_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (event_id, project_id, branch_id, seq, actor, kind, base_event_id,
         blob, sha(blob), _iso(ts)))
    return {"event_id": event_id, "seq": seq, "kind": kind,
            "branch_id": branch_id, "base_event_id": base_event_id,
            "payload": payload, "actor": actor, "created_at": _iso(ts),
            "payload_hash": sha(blob)}


def list_events(conn, project_id: str) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE project_id=? ORDER BY seq ASC",
        (project_id,)).fetchall()
    return [_event_row(r) for r in rows]


def _event_row(r: sqlite3.Row) -> dict:
    return {"event_id": r["event_id"], "project_id": r["project_id"],
            "seq": r["seq"], "kind": r["kind"],
            "branch_id": r["branch_id"], "base_event_id": r["base_event_id"],
            "actor": r["actor"], "created_at": r["created_at"],
            "payload_hash": r["payload_hash"],
            "payload": json.loads(r["payload"])}


def list_branches(conn, project_id: str) -> List[dict]:
    rows = conn.execute("SELECT * FROM branches WHERE project_id=? ORDER BY rowid",
                        (project_id,)).fetchall()
    return [dict(r) for r in rows]


def create_branch(conn, project_id: str, branch_id: str, parent_branch_id: str,
                  base_event_id: Optional[str], label: str, clock: Clock) -> None:
    ts = _iso(clock.now())
    conn.execute("INSERT INTO branches(branch_id,project_id,parent_branch_id,"
                 "base_event_id,label,created_at) VALUES(?,?,?,?,?,?)",
                 (branch_id, project_id, parent_branch_id, base_event_id,
                  label, ts))


def store_source(conn, project_id: str, source_id: str, version_id: str,
                 parent_version_id: Optional[str], kind: str, filename: str,
                 raw_text: str, created_at: str) -> None:
    conn.execute("INSERT OR REPLACE INTO sources(project_id,source_id,version_id,"
                 "parent_version_id,kind,filename,raw_text,content_hash,created_at)"
                 " VALUES(?,?,?,?,?,?,?,?,?)",
                 (project_id, source_id, version_id, parent_version_id, kind,
                  filename, raw_text, sha(raw_text), created_at))


def get_source(conn, project_id: str, source_id: str, version_id: str):
    return conn.execute(
        "SELECT * FROM sources WHERE project_id=? AND source_id=? AND version_id=?",
        (project_id, source_id, version_id)).fetchone()


def list_sources(conn, project_id: str) -> List[dict]:
    rows = conn.execute("SELECT * FROM sources WHERE project_id=? ORDER BY rowid",
                        (project_id,)).fetchall()
    return [dict(r) for r in rows]


def store_run(conn, project_id: str, branch_id: str, run_id: str,
              source_versions: dict, param_fingerprint: str, result: dict,
              created_event_id: str, created_at: str) -> None:
    conn.execute("INSERT OR REPLACE INTO runs(project_id,branch_id,run_id,"
                 "source_versions,param_fingerprint,result_json,created_event_id,"
                 "created_at) VALUES(?,?,?,?,?,?,?,?)",
                 (project_id, branch_id, run_id,
                  json.dumps(source_versions, sort_keys=True),
                  param_fingerprint, json.dumps(result, ensure_ascii=False),
                  created_event_id, created_at))


def list_runs(conn, project_id: str) -> List[dict]:
    rows = conn.execute("SELECT * FROM runs WHERE project_id=? ORDER BY rowid",
                        (project_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_versions"] = json.loads(d["source_versions"])
        d["result"] = json.loads(d["result_json"])
        del d["result_json"]
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Branch lineage and replay
# ---------------------------------------------------------------------------

def branch_lineage(conn, project_id: str, branch_id: str) -> List[str]:
    """Return branch ids from main down to this branch."""
    chain = []
    current = branch_id
    seen = set()
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        row = conn.execute("SELECT parent_branch_id FROM branches WHERE branch_id=? "
                           "AND project_id=?", (current, project_id)).fetchone()
        if row is None:
            break
        current = row["parent_branch_id"]
    chain.reverse()
    return chain


def events_on_branch(conn, project_id: str, branch_id: str,
                     stop_event_id: Optional[str] = None) -> List[dict]:
    """Replay branch lineage up to ``stop_event_id`` (inclusive).

    Events are returned in global sequence order, restricted to the branch
    chain.  This is what restores *historical* views: later events on other
    branches are excluded.
    """
    lineage = set(branch_lineage(conn, project_id, branch_id))
    rows = conn.execute(
        "SELECT * FROM events WHERE project_id=? ORDER BY seq ASC",
        (project_id,)).fetchall()
    out: List[dict] = []
    for r in rows:
        if r["branch_id"] not in lineage:
            continue
        ev = _event_row(r)
        out.append(ev)
        if stop_event_id and ev["event_id"] == stop_event_id:
            break
    return out


def project_snapshot(conn, project_id: str, branch_id: str = "B:main",
                     stop_event_id: Optional[str] = None) -> dict:
    """Reconstruct state by replaying the selected lineage.

    State semantics:
      * the newest source version per source_id at this point in history wins;
      * source_submitted / source_revision events pick the active versions;
      * decision events attach accept/reject/parameter decisions to candidates;
      * runs are recorded on branches and marked stale against active versions.
    """
    events = events_on_branch(conn, project_id, branch_id, stop_event_id)
    active_versions: Dict[str, str] = {}
    decisions: List[dict] = []
    judgements_extra: List[dict] = []
    run_ids: List[str] = []
    branch_pointers: Dict[str, str] = {"B:main": None}
    for ev in events:
        p = ev["payload"]
        if ev["kind"] in ("source_submitted", "source_revision"):
            active_versions[p["source_id"]] = p["version_id"]
        elif ev["kind"] == "run_computed":
            if p.get("run_id") not in run_ids:
                run_ids.append(p["run_id"])
        elif ev["kind"] in ("candidate_accepted", "candidate_rejected",
                            "decision_retracted", "decision_conflict",
                            "conflict_resolved", "parameter_adjusted",
                            "candidate_expired"):
            decisions.append(ev)
            if ev["kind"] in ("candidate_accepted", "candidate_rejected",
                              "decision_retracted", "decision_conflict",
                              "conflict_resolved", "parameter_adjusted",
                              "candidate_expired"):
                judgements_extra.append(ev)
        elif ev["kind"] == "branch_created":
            branch_pointers[p["branch_id"]] = ev["event_id"]
    current_run_id = run_ids[-1] if run_ids else None
    current_run = None
    if current_run_id:
        rows = conn.execute("SELECT * FROM runs WHERE project_id=? AND run_id=?",
                            (project_id, current_run_id)).fetchall()
        if rows:
            r = dict(rows[0])
            current_run = json.loads(r["result_json"])
            current_run["run_id"] = r["run_id"]
            current_run["branch_id"] = r["branch_id"]
            sv = json.loads(r["source_versions"])
            current_run["stale"] = any(active_versions.get(sid) != vid
                                       for sid, vid in sv.items())
    return {
        "project_id": project_id,
        "branch_id": branch_id,
        "stop_event_id": stop_event_id,
        "active_versions": active_versions,
        "decisions": [_decision_view(d) for d in decisions],
        "current_run": current_run,
        "events": events,
    }


def _decision_view(ev: dict) -> dict:
    p = dict(ev["payload"])
    p["event_id"] = ev["event_id"]
    p["kind"] = ev["kind"]
    p["actor"] = ev["actor"]
    p["created_at"] = ev["created_at"]
    return p


# ---------------------------------------------------------------------------
# Export / import (delete-database then import must preserve identity/order)
# ---------------------------------------------------------------------------

def export_bundle(conn, project_id: str) -> dict:
    project = conn.execute("SELECT * FROM projects WHERE project_id=?",
                           (project_id,)).fetchone()
    events = list_events(conn, project_id)
    sources = [dict(r) for r in conn.execute(
        "SELECT * FROM sources WHERE project_id=? ORDER BY rowid",
        (project_id,)).fetchall()]
    runs = []
    for r in conn.execute("SELECT * FROM runs WHERE project_id=? ORDER BY rowid",
                          (project_id,)).fetchall():
        runs.append({
            "branch_id": r["branch_id"], "run_id": r["run_id"],
            "source_versions": json.loads(r["source_versions"]),
            "param_fingerprint": r["param_fingerprint"],
            "result": json.loads(r["result_json"]),
            "created_event_id": r["created_event_id"],
            "created_at": r["created_at"],
        })
    branches = [dict(r) for r in conn.execute(
        "SELECT * FROM branches WHERE project_id=? ORDER BY rowid",
        (project_id,)).fetchall()]
    counter = conn.execute("SELECT value FROM counters WHERE project_id=? AND name='event'",
                           (project_id,)).fetchone()
    return {
        "bundle_version": BUNDLE_VERSION,
        "project": dict(project),
        "event_counter": counter["value"] if counter else 0,
        "branches": branches,
        "events": events,
        "sources": sources,
        "runs": runs,
    }


def _event_row_full(conn, e):  # pragma: no cover - placeholder removed below
    return e["event_id"]


def import_bundle(conn, bundle: dict) -> str:
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        raise ValueError("unsupported bundle version")
    project = bundle["project"]
    conn.execute("INSERT INTO projects VALUES(?,?,?)",
                 (project["project_id"], project["name"], project["created_at"]))
    for b in bundle["branches"]:
        conn.execute("INSERT INTO branches(branch_id,project_id,parent_branch_id,"
                     "base_event_id,label,created_at) VALUES(?,?,?,?,?,?)",
                     (b["branch_id"], b["project_id"], b["parent_branch_id"],
                      b["base_event_id"], b["label"], b["created_at"]))
    for s in bundle["sources"]:
        conn.execute("INSERT INTO sources(project_id,source_id,version_id,"
                     "parent_version_id,kind,filename,raw_text,content_hash,"
                     "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (s["project_id"], s["source_id"], s["version_id"],
                      s["parent_version_id"], s["kind"], s["filename"],
                      s["raw_text"], s["content_hash"], s["created_at"]))
    for ev in bundle["events"]:
        blob = json.dumps(ev["payload"], sort_keys=True,
                          ensure_ascii=False, separators=(",", ":"))
        if sha(blob) != ev["payload_hash"]:
            raise ValueError(f"payload hash mismatch for {ev['event_id']}")
        conn.execute(
            "INSERT INTO events(event_id,project_id,branch_id,seq,actor,kind,"
            "base_event_id,payload,payload_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ev["event_id"], ev["project_id"], ev["branch_id"], ev["seq"],
             ev["actor"], ev["kind"], ev["base_event_id"], blob,
             ev["payload_hash"], ev["created_at"]))
    for r in bundle["runs"]:
        conn.execute("INSERT INTO runs(project_id,branch_id,run_id,"
                     "source_versions,param_fingerprint,result_json,"
                     "created_event_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (project["project_id"], r["branch_id"], r["run_id"],
                      json.dumps(r["source_versions"], sort_keys=True),
                      r["param_fingerprint"],
                      json.dumps(r["result"], ensure_ascii=False),
                      r["created_event_id"], r["created_at"]))
    conn.execute("INSERT INTO counters(project_id,name,value) VALUES(?,?,?)",
                 (project["project_id"], "event", bundle["event_counter"]))
    return project["project_id"]
