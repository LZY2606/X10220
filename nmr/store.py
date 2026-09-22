import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project (
    uid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branch (
    uid TEXT PRIMARY KEY,
    project_uid TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_branch_uid TEXT,
    forked_from_event_seq INTEGER,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    uid TEXT PRIMARY KEY,
    project_uid TEXT NOT NULL,
    branch_uid TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    created TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(project_uid, branch_uid, seq)
);
CREATE INDEX IF NOT EXISTS idx_event_project ON event(project_uid);
"""

EVENT_KINDS = (
    "project_created",
    "source_imported",
    "param_revision",
    "run_generated",
    "review_decision",
    "conflict_detected",
    "conflict_resolved",
    "decision_retracted",
    "branch_created",
    "import_bundle",
)


class Store:
    def __init__(self, path=":memory:"):
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- meta / counters -------------------------------------------------
    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, sort_keys=True)),
        )
        self.conn.commit()

    def next_event_seq(self, project_uid):
        key = "event_seq:%s" % project_uid
        nxt = self.get_meta(key, 0) + 1
        self.set_meta(key, nxt)
        return nxt

    # ---- project ---------------------------------------------------------
    def create_project(self, uid, name, created):
        self.conn.execute(
            "INSERT INTO project(uid,name,created) VALUES(?,?,?)", (uid, name, created)
        )
        self.conn.commit()

    def get_project(self, uid):
        row = self.conn.execute("SELECT * FROM project WHERE uid=?", (uid,)).fetchone()
        return dict(row) if row else None

    def list_projects(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM project ORDER BY created")]

    # ---- branch ----------------------------------------------------------
    def add_branch(self, uid, project_uid, name, parent_branch_uid, forked_from_event_seq, created):
        self.conn.execute(
            "INSERT INTO branch(uid,project_uid,name,parent_branch_uid,forked_from_event_seq,created)"
            " VALUES(?,?,?,?,?,?)",
            (uid, project_uid, name, parent_branch_uid, forked_from_event_seq, created),
        )
        self.conn.commit()

    def get_branch(self, uid):
        row = self.conn.execute("SELECT * FROM branch WHERE uid=?", (uid,)).fetchone()
        return dict(row) if row else None

    def list_branches(self, project_uid):
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM branch WHERE project_uid=? ORDER BY created", (project_uid,)
            )
        ]

    # ---- events ----------------------------------------------------------
    def insert_event(self, uid, project_uid, branch_uid, seq, kind, actor, created, payload):
        if kind not in EVENT_KINDS:
            raise ValueError("unknown event kind %s" % kind)
        self.conn.execute(
            "INSERT INTO event(uid,project_uid,branch_uid,seq,kind,actor,created,payload)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (uid, project_uid, branch_uid, seq, kind, actor, created,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        self.conn.commit()

    def list_project_events(self, project_uid):
        rows = self.conn.execute(
            "SELECT * FROM event WHERE project_uid=? ORDER BY seq", (project_uid,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    # ---- whole database snapshot (export / import) -----------------------
    def export_db(self):
        def dump(table):
            return [dict(r) for r in self.conn.execute("SELECT * FROM %s" % table)]

        return {
            "format": "nmr-workbench-db",
            "format_version": 1,
            "meta": dump("meta"),
            "project": dump("project"),
            "branch": dump("branch"),
            "event": dump("event"),
        }

    def import_db(self, snapshot, merge=False):
        if snapshot.get("format") != "nmr-workbench-db":
            raise ValueError("not an nmr-workbench database export")
        if not merge:
            self.conn.executescript(
                "DELETE FROM event; DELETE FROM branch; DELETE FROM project; DELETE FROM meta;"
            )
        for row in snapshot["meta"]:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (row["key"], row["value"]),
            )
        for row in snapshot["project"]:
            self.conn.execute(
                "INSERT INTO project(uid,name,created) VALUES(?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET name=excluded.name, created=excluded.created",
                (row["uid"], row["name"], row["created"]),
            )
        for row in snapshot["branch"]:
            self.conn.execute(
                "INSERT INTO branch(uid,project_uid,name,parent_branch_uid,forked_from_event_seq,created)"
                " VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET name=excluded.name,"
                " parent_branch_uid=excluded.parent_branch_uid,"
                " forked_from_event_seq=excluded.forked_from_event_seq, created=excluded.created",
                (row["uid"], row["project_uid"], row["name"], row["parent_branch_uid"],
                 row["forked_from_event_seq"], row["created"]),
            )
        for row in snapshot["event"]:
            self.conn.execute(
                "INSERT INTO event(uid,project_uid,branch_uid,seq,kind,actor,created,payload)"
                " VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(uid) DO NOTHING",
                (row["uid"], row["project_uid"], row["branch_uid"], row["seq"], row["kind"],
                 row["actor"], row["created"], row["payload"]),
            )
        self.conn.commit()
