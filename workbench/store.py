"""Append-only event store on SQLite.

Raw inputs are immutable once submitted; revisions are new events that
reference their parent version. Every event carries a stable identity
(evt-NNNNNN) derived from its global sequence number, so export -> delete
-> import preserves identity, order and provenance relations.
"""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches(
  branch_id   TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  parent_branch TEXT,
  fork_event  TEXT,
  created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  seq      INTEGER PRIMARY KEY,
  event_id TEXT UNIQUE NOT NULL,
  branch   TEXT NOT NULL,
  parents  TEXT NOT NULL,
  kind     TEXT NOT NULL,
  actor    TEXT NOT NULL,
  vtime    INTEGER NOT NULL,
  payload  TEXT NOT NULL
);
"""

EXPORT_FORMAT = "nmrwb-export/1"


class Store:
    def __init__(self, path="workbench.db", clock=None):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Injectable virtual clock; tests pass a deterministic counter.
        self.clock = clock or (lambda: 0)
        if not self.conn.execute("SELECT 1 FROM branches LIMIT 1").fetchone():
            self.conn.execute(
                "INSERT INTO branches VALUES('main','main',NULL,NULL,0)")
            self.conn.commit()

    # ------------------------------------------------------------- branches
    def branches(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM branches ORDER BY created_seq, branch_id")]

    def create_branch(self, name, fork_event):
        n = self.conn.execute("SELECT COUNT(*) AS c FROM branches").fetchone()["c"]
        branch_id = f"br-{n:04d}"
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0) AS s FROM events").fetchone()["s"]
        parent = None
        if fork_event:
            row = self.conn.execute(
                "SELECT branch FROM events WHERE event_id=?",
                (fork_event,)).fetchone()
            parent = row["branch"] if row else None
        self.conn.execute(
            "INSERT INTO branches VALUES(?,?,?,?,?)",
            (branch_id, name, parent, fork_event, seq))
        self.conn.commit()
        return branch_id

    # --------------------------------------------------------------- events
    def head(self, branch="main"):
        row = self.conn.execute(
            "SELECT event_id FROM events WHERE branch=? "
            "ORDER BY seq DESC LIMIT 1", (branch,)).fetchone()
        if row:
            return row["event_id"]
        br = self.conn.execute(
            "SELECT fork_event FROM branches WHERE branch_id=?",
            (branch,)).fetchone()
        return br["fork_event"] if br else None

    def append(self, branch, kind, actor, payload, parents=None):
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS s FROM events").fetchone()["s"]
        event_id = f"evt-{seq:06d}"
        if parents is None:
            h = self.head(branch)
            parents = [h] if h else []
        self.conn.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
            (seq, event_id, branch, json.dumps(parents), kind, actor,
             int(self.clock()), json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()
        return event_id

    def get_event(self, event_id):
        row = self.conn.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def events(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM events ORDER BY seq")]

    def ancestry(self, event_id):
        """All events reachable through parent links, ordered by seq."""
        if not event_id:
            return []
        by_id = {r["event_id"]: dict(r)
                 for r in self.conn.execute("SELECT * FROM events")}
        seen, out, stack = set(), [], [event_id]
        while stack:
            eid = stack.pop()
            if not eid or eid in seen or eid not in by_id:
                continue
            seen.add(eid)
            out.append(by_id[eid])
            stack.extend(json.loads(by_id[eid]["parents"]))
        return sorted(out, key=lambda e: e["seq"])

    def branch_events(self, branch="main", at_event=None):
        return self.ancestry(at_event or self.head(branch))

    # --------------------------------------------------------- export/import
    def export(self):
        return {
            "format": EXPORT_FORMAT,
            "branches": self.branches(),
            "events": self.events(),
        }

    def import_dump(self, dump, mode="replace"):
        if dump.get("format") != EXPORT_FORMAT:
            raise ValueError(f"unsupported export format: {dump.get('format')!r}")
        if mode == "replace":
            self.conn.executescript("DELETE FROM events; DELETE FROM branches;")
        for b in dump["branches"]:
            self.conn.execute(
                "INSERT OR IGNORE INTO branches VALUES(?,?,?,?,?)",
                (b["branch_id"], b["name"], b.get("parent_branch"),
                 b.get("fork_event"), b["created_seq"]))
        for e in dump["events"]:
            parents = e["parents"] if isinstance(e["parents"], str) \
                else json.dumps(e["parents"])
            payload = e["payload"] if isinstance(e["payload"], str) \
                else json.dumps(e["payload"], ensure_ascii=False)
            self.conn.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?)",
                (e["seq"], e["event_id"], e["branch"], parents, e["kind"],
                 e["actor"], e["vtime"], payload))
        self.conn.commit()

    def close(self):
        self.conn.close()
