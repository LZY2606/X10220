import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  branch TEXT NOT NULL DEFAULT 'main',
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, seq);
"""


class Event:
    def __init__(self, row):
        self.id = row["id"]
        self.seq = row["seq"]
        self.project_id = row["project_id"]
        self.branch = row["branch"]
        self.type = row["type"]
        self.payload = json.loads(row["payload"])
        self.created_at = row["created_at"]

    def to_dict(self):
        return {
            "id": self.id,
            "seq": self.seq,
            "project_id": self.project_id,
            "branch": self.branch,
            "type": self.type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def _next_seq(self):
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS s FROM events").fetchone()
        return row["s"]

    def append(self, project_id, type_, payload, created_at, branch="main",
               event_id=None, seq=None):
        if seq is None:
            seq = self._next_seq()
        eid = event_id or "E%06d" % seq
        self.conn.execute(
            "INSERT INTO events(id,seq,project_id,branch,type,payload,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (eid, seq, project_id, branch, type_,
             json.dumps(payload, sort_keys=True, ensure_ascii=False), created_at))
        self.conn.commit()
        return eid

    def has_event(self, event_id):
        row = self.conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone()
        return row is not None

    def has_seq(self, seq):
        row = self.conn.execute("SELECT 1 FROM events WHERE seq=?", (seq,)).fetchone()
        return row is not None

    def event(self, event_id):
        row = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return Event(row) if row else None

    def events(self, project_id=None, upto_seq=None):
        sql = "SELECT * FROM events WHERE 1=1"
        args = []
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        if upto_seq is not None:
            sql += " AND seq<=?"
            args.append(upto_seq)
        sql += " ORDER BY seq"
        return [Event(r) for r in self.conn.execute(sql, args).fetchall()]

    def add_project(self, pid, name, created_at):
        self.conn.execute(
            "INSERT OR IGNORE INTO projects(id,name,created_at) VALUES(?,?,?)",
            (pid, name, created_at))
        self.conn.commit()

    def projects(self):
        rows = self.conn.execute("SELECT * FROM projects ORDER BY created_at, id").fetchall()
        return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"]} for r in rows]
