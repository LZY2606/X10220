"""Event-sourced SQLite storage.

Immutable append-only event log:
  * events are never overwritten - source revisions become new documents with a
    parent version, edits become new events on a branch;
  * each event id is a stable hash of (project, seq, parent, payload, ts, author)
    so exporting, deleting the database and re-importing reproduces identical
    ids and order;
  * decisions are confirm/reject events; concurrent divergent decisions on the
    same candidate are recorded as a conflict with both branches preserved.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional, List, Dict, Any, Tuple

from .fingerprint import sha256_text, fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  project_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_id TEXT PRIMARY KEY,
  parent_id TEXT,
  branch TEXT NOT NULL,
  ts REAL NOT NULL,
  author TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_project ON events(project_id, seq);
CREATE TABLE IF NOT EXISTS kvmeta (
  project_id TEXT, key TEXT, value TEXT,
  PRIMARY KEY(project_id, key)
);
"""


class Clock:
    """Virtual time for tests; real monotonic-ish UTC time in production."""

    def __init__(self, start=1_700_000_000.0, step=1.0):
        self.t = start
        self.step = step

    def now(self) -> float:
        v = self.t
        self.t += self.step
        return v


def event_id(project_id, seq, parent_id, branch, ts, author, kind, payload_hash):
    basis = f"{project_id}|{seq}|{parent_id or ''}|{branch}|{ts:.6f}|{author}|" \
            f"{kind}|{payload_hash}"
    return "ev_" + sha256_text(basis)[:16]


class EventStore:
    def __init__(self, path: Optional[str] = None, clock: Optional[Clock] = None):
        self.path = path or ":memory:"
        self.clock = clock or Clock(step=0.0)
        if self.clock.step == 0.0 and path not in (None, ":memory:"):
            pass  # production caller can pass a real clock
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- projects ----------------------------------------------------------
    def create_project(self, project_id, name, now=None):
        ts = self.clock.now() if now is None else now
        self.conn.execute(
            "INSERT OR IGNORE INTO projects(project_id,name,created_ts) VALUES(?,?,?)",
            (project_id, name, ts))
        self.conn.commit()
        return project_id

    def list_projects(self):
        rows = self.conn.execute("SELECT * FROM projects ORDER BY created_ts").fetchall()
        return [dict(r) for r in rows]

    def get_project(self, project_id):
        r = self.conn.execute("SELECT * FROM projects WHERE project_id=?",
                              (project_id,)).fetchone()
        return dict(r) if r else None

    # -- branch helpers ----------------------------------------------------
    def branches(self, project_id):
        rows = self.conn.execute(
            "SELECT branch, COUNT(*) n, MAX(seq) maxseq FROM events "
            "WHERE project_id=? GROUP BY branch ORDER BY MIN(seq)", (project_id,)
        ).fetchall()
        return {r["branch"]: {"count": r["n"], "max_seq": r["maxseq"]} for r in rows}

    def branch_tip(self, project_id, branch):
        r = self.conn.execute(
            "SELECT event_id, seq FROM events WHERE project_id=? AND branch=? "
            "ORDER BY seq DESC LIMIT 1", (project_id, branch)).fetchone()
        return (r["event_id"], r["seq"]) if r else (None, -1)

    def get_event(self, event_id):
        r = self.conn.execute("SELECT * FROM events WHERE event_id=?",
                              (event_id,)).fetchone()
        return self._row(r) if r else None

    def _row(self, r):
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        return d

    def events_for_branch(self, project_id, branch, upto_seq=None):
        q = "SELECT * FROM events WHERE project_id=? AND branch=? "
        args = [project_id, branch]
        if upto_seq is not None:
            q += "AND seq<=? "
            args.append(upto_seq)
        q += "ORDER BY seq"
        return [self._row(r) for r in self.conn.execute(q, args).fetchall()]

    def all_events(self, project_id):
        return [self._row(r) for r in self.conn.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq, branch",
            (project_id,)).fetchall()]

    def next_seq(self, project_id, parent_id=None):
        if parent_id is None:
            r = self.conn.execute(
                "SELECT COALESCE(MAX(seq),-1)+1 s FROM events WHERE project_id=?",
                (project_id,)).fetchone()
            return int(r["s"])
        r = self.conn.execute("SELECT seq, branch FROM events WHERE event_id=?",
                              (parent_id,)).fetchone()
        if not r:
            raise KeyError(f"parent event {parent_id} not found")
        mx = self.conn.execute(
            "SELECT COALESCE(MAX(seq),-1) m FROM events WHERE project_id=?",
            (project_id,)).fetchone()
        return max(int(r["seq"]), int(mx["m"])) + 1

    def append(self, project_id, kind, payload, author="reviewer",
               branch="main", parent_id=None, ts=None):
        ts = self.clock.now() if ts is None else ts
        pj = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"), default=str)
        phash = sha256_text(pj)
        if parent_id is None:
            parent_id, _ = self.branch_tip(project_id, branch)
        seq = self.next_seq(project_id, None)
        eid = event_id(project_id, seq, parent_id, branch, ts, author, kind, phash)
        self.conn.execute(
            "INSERT INTO events(project_id,seq,event_id,parent_id,branch,ts,author,"
            "kind,payload,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (project_id, seq, eid, parent_id, branch, ts, author, kind, pj, phash))
        self.conn.commit()
        return self.get_event(eid)

    # -- export / import (deterministic round trip) ------------------------
    def export_project(self, project_id):
        proj = self.get_project(project_id)
        events = self.all_events(project_id)
        bundle = {"format": "nmr-workbench-export/1",
                  "project": proj,
                  "events": [{k: v for k, v in e.items() if k != "payload"}
                             for e in events],
                  "payloads": [e["payload"] for e in events]}
        bundle["bundle_hash"] = sha256_text(
            json.dumps([e["event_id"] for e in events]))
        return bundle

    def import_project(self, bundle, conflict="error"):
        pid = bundle["project"]["project_id"]
        if self.get_project(pid):
            if conflict == "error":
                raise ValueError(f"project {pid} already present")
            return pid
        self.create_project(pid, bundle["project"]["name"],
                            bundle["project"]["created_ts"])
        for ev, payload in zip(bundle["events"], bundle["payloads"]):
            pj = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                            separators=(",", ":"), default=str)
            self.conn.execute(
                "INSERT INTO events(project_id,seq,event_id,parent_id,branch,ts,author,"
                "kind,payload,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ev["project_id"], ev["seq"], ev["event_id"], ev["parent_id"],
                 ev["branch"], ev["ts"], ev["author"], ev["kind"], pj,
                 ev["payload_hash"]))
        self.conn.commit()
        # verify hash chain
        for ev, payload in zip(bundle["events"], bundle["payloads"]):
            expect = event_id(ev["project_id"], ev["seq"], ev["parent_id"],
                              ev["branch"], ev["ts"], ev["author"], ev["kind"],
                              sha256_text(json.dumps(
                                  payload, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":"), default=str)))
            if expect != ev["event_id"]:
                raise ValueError("event hash chain verification failed")
        return pid
