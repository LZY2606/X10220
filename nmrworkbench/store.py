"""SQLite-backed event-sourced store with a deterministic virtual clock.

Raw inputs are immutable: a revision is stored as a new row with a parent
pointer.  All derived state (candidates, staleness, decisions, conflicts) is
reconstructed by replaying the event chain, so checking out any historical
event restores the dependency versions and visible conclusions of that moment.
"""
import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active_tip INTEGER NOT NULL DEFAULT 0,
    created_tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS inputs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    parent_seq INTEGER NOT NULL DEFAULT 0,
    branch TEXT NOT NULL DEFAULT 'main',
    kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    payload TEXT NOT NULL,
    tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    tip_seq INTEGER NOT NULL,
    created_from_seq INTEGER NOT NULL,
    PRIMARY KEY (project_id, name)
);
"""

EPOCH = "2026-01-01T00:00:00Z"


def _iso_from_tick(tick):
    base = 1767225600  # 2026-01-01T00:00:00Z
    import datetime

    ts = datetime.datetime.utcfromtimestamp(base + tick)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path=":memory:"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='tick'").fetchone()
        self._tick = int(row["value"]) if row else 0

    def close(self):
        self.conn.close()

    # -- virtual clock -----------------------------------------------------
    def tick(self):
        """Advance the deterministic virtual clock and return (tick, iso)."""
        self._tick += 1
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('tick',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(self._tick),))
        self.conn.commit()
        return self._tick, _iso_from_tick(self._tick)

    # -- projects ----------------------------------------------------------
    def create_project(self, project_id, name):
        tick, _ = self.tick()
        self.conn.execute(
            "INSERT INTO projects(id,name,active_tip,created_tick) VALUES(?,?,0,?)",
            (project_id, name, tick))
        self.conn.execute(
            "INSERT INTO branches(project_id,name,tip_seq,created_from_seq) "
            "VALUES(?,?,0,0)", (project_id, "main"))
        self.conn.commit()
        return project_id

    def list_projects(self):
        rows = self.conn.execute(
            "SELECT id,name,active_tip,created_tick FROM projects ORDER BY created_tick"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_project(self, project_id):
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    # -- immutable raw inputs ----------------------------------------------
    def put_input(self, project_id, kind, content, source, input_id=None,
                  parent_id=None):
        existing = self.conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM inputs "
            "WHERE project_id=? AND kind=?", (project_id, kind)).fetchone()
        version = existing["v"] + 1
        if input_id is None:
            input_id = "in-%s-v%d" % (kind, version)
        if parent_id is None and version > 1:
            prev = self.conn.execute(
                "SELECT id FROM inputs WHERE project_id=? AND kind=? AND version=?",
                (project_id, kind, version - 1)).fetchone()
            parent_id = prev["id"] if prev else None
        tick, _ = self.tick()
        self.conn.execute(
            "INSERT INTO inputs(id,project_id,kind,version,parent_id,source,content,tick)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (input_id, project_id, kind, version, parent_id, source,
             json.dumps(content, ensure_ascii=False, sort_keys=True), tick))
        self.conn.commit()
        return self.get_input(input_id)

    def get_input(self, input_id):
        row = self.conn.execute(
            "SELECT * FROM inputs WHERE id=?", (input_id,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["content"] = json.loads(rec["content"])
        return rec

    def inputs_of(self, project_id):
        rows = self.conn.execute(
            "SELECT * FROM inputs WHERE project_id=? ORDER BY kind, version",
            (project_id,)).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["content"] = json.loads(rec["content"])
            out.append(rec)
        return out

    # -- events --------------------------------------------------------------
    def append_event(self, project_id, kind, payload, actor="system",
                     branch=None, parent_seq=None):
        proj = self.get_project(project_id)
        if proj is None:
            raise KeyError("unknown project %s" % project_id)
        if parent_seq is None:
            parent_seq = proj["active_tip"]
        if branch is None:
            branch = self._branch_for_tip(project_id, parent_seq)
        tick, iso = self.tick()
        cur = self.conn.execute(
            "INSERT INTO events(project_id,parent_seq,branch,kind,actor,payload,tick)"
            " VALUES(?,?,?,?,?,?,?)",
            (project_id, parent_seq, branch, kind, actor,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), tick))
        seq = cur.lastrowid
        self.conn.execute(
            "UPDATE projects SET active_tip=? WHERE id=?", (seq, project_id))
        self.conn.execute(
            "UPDATE branches SET tip_seq=? WHERE project_id=? AND name=?",
            (seq, project_id, branch))
        self.conn.commit()
        ev = self.get_event(seq)
        ev["ts"] = iso
        return ev

    def _branch_for_tip(self, project_id, tip_seq):
        row = self.conn.execute(
            "SELECT name FROM branches WHERE project_id=? AND tip_seq=? "
            "ORDER BY name LIMIT 1", (project_id, tip_seq)).fetchone()
        if row:
            return row["name"]
        # tip_seq is not any branch tip: we are forking a new branch.
        name = "branch-from-%d" % tip_seq
        suffix = 1
        base = name
        while self.conn.execute(
                "SELECT 1 FROM branches WHERE project_id=? AND name=?",
                (project_id, name)).fetchone():
            suffix += 1
            name = "%s-%d" % (base, suffix)
        self.conn.execute(
            "INSERT INTO branches(project_id,name,tip_seq,created_from_seq)"
            " VALUES(?,?,0,?)", (project_id, name, tip_seq))
        return name

    def get_event(self, seq):
        row = self.conn.execute(
            "SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
        if not row:
            return None
        ev = dict(row)
        ev["payload"] = json.loads(ev["payload"])
        ev["ts"] = _iso_from_tick(ev["tick"])
        return ev

    def checkout(self, project_id, seq):
        """Move the active pointer to a historical event (replayable, no data loss)."""
        if seq != 0 and self.get_event(seq) is None:
            raise KeyError("unknown event seq %s" % seq)
        self.conn.execute(
            "UPDATE projects SET active_tip=? WHERE id=?", (seq, project_id))
        self.conn.commit()
        return self.get_project(project_id)

    def branches_of(self, project_id):
        rows = self.conn.execute(
            "SELECT * FROM branches WHERE project_id=? ORDER BY name",
            (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def chain_to(self, seq):
        """Return the event chain from root to seq (inclusive), in order."""
        chain = []
        cur = seq
        while cur:
            ev = self.get_event(cur)
            if ev is None:
                break
            chain.append(ev)
            cur = ev["parent_seq"]
        chain.reverse()
        return chain

    # -- export / import -----------------------------------------------------
    def export_project(self, project_id):
        proj = self.get_project(project_id)
        if proj is None:
            raise KeyError("unknown project %s" % project_id)
        events = [self.get_event(r["seq"]) for r in self.conn.execute(
            "SELECT seq FROM events WHERE project_id=? ORDER BY seq",
            (project_id,)).fetchall()]
        return {
            "format": "nmr-workbench-export",
            "format_version": 1,
            "project": proj,
            "inputs": self.inputs_of(project_id),
            "events": events,
            "branches": self.branches_of(project_id),
        }

    def import_project(self, bundle):
        if bundle.get("format") != "nmr-workbench-export":
            raise ValueError("unrecognised export format")
        proj = bundle["project"]
        if self.get_project(proj["id"]) is not None:
            raise ValueError("project %s already exists" % proj["id"])
        self.conn.execute(
            "INSERT INTO projects(id,name,active_tip,created_tick) VALUES(?,?,?,?)",
            (proj["id"], proj["name"], proj["active_tip"], proj["created_tick"]))
        for inp in bundle["inputs"]:
            self.conn.execute(
                "INSERT INTO inputs(id,project_id,kind,version,parent_id,source,content,tick)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (inp["id"], inp["project_id"], inp["kind"], inp["version"],
                 inp["parent_id"], inp["source"],
                 json.dumps(inp["content"], ensure_ascii=False, sort_keys=True),
                 inp["tick"]))
        for ev in bundle["events"]:
            self.conn.execute(
                "INSERT INTO events(seq,project_id,parent_seq,branch,kind,actor,payload,tick)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (ev["seq"], ev["project_id"], ev["parent_seq"], ev["branch"],
                 ev["kind"], ev["actor"],
                 json.dumps(ev["payload"], ensure_ascii=False, sort_keys=True),
                 ev["tick"]))
        for br in bundle["branches"]:
            self.conn.execute(
                "INSERT INTO branches(project_id,name,tip_seq,created_from_seq)"
                " VALUES(?,?,?,?)",
                (br["project_id"], br["name"], br["tip_seq"],
                 br["created_from_seq"]))
        max_tick = max([proj["created_tick"]]
                       + [i["tick"] for i in bundle["inputs"]]
                       + [e["tick"] for e in bundle["events"]])
        if max_tick > self._tick:
            self._tick = max_tick
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('tick',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self._tick),))
        self.conn.commit()
        return proj["id"]
