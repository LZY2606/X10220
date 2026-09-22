"""SQLite-backed event store.

Raw inputs are immutable once submitted; revisions are new versions linked to
their parent. Every derived artifact records algorithm version, parameter
fingerprint and direct sources so a project can be replayed step by step.
"""
import json
import sqlite3
from pathlib import Path

from . import clock
from .ids import digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  project_id TEXT PRIMARY KEY, name TEXT, seq_counter INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS inputs(
  input_id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, version INTEGER,
  parent_id TEXT, filename TEXT, raw_text TEXT, sha256 TEXT,
  meta TEXT, items TEXT, changed_items TEXT, seq INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS params(
  param_id TEXT PRIMARY KEY, project_id TEXT, name TEXT, version INTEGER,
  parent_id TEXT, payload TEXT, fingerprint TEXT, changed_sites TEXT,
  seq INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY, project_id TEXT, seq INTEGER, branch_id TEXT,
  kind TEXT, actor TEXT, payload TEXT, created_at TEXT, parent_event_id TEXT
);
CREATE TABLE IF NOT EXISTS derived(
  derived_id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, algo_version TEXT,
  fingerprint TEXT, deps TEXT, payload TEXT, seq INTEGER, created_at TEXT
);
"""

TABLES = ["projects", "inputs", "params", "events", "derived"]


def _j(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- projects -----------------------------------------------------
    def create_project(self, project_id, name):
        self.conn.execute(
            "INSERT OR IGNORE INTO projects VALUES(?,?,0,?)",
            (project_id, name, clock.now()),
        )
        self.conn.commit()
        return project_id

    def get_project(self, project_id):
        row = self.conn.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_projects(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM projects ORDER BY created_at")]

    def _next_seq(self, project_id):
        self.conn.execute(
            "UPDATE projects SET seq_counter=seq_counter+1 WHERE project_id=?",
            (project_id,),
        )
        seq = self.conn.execute(
            "SELECT seq_counter FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        self.conn.commit()
        return seq

    # ---- inputs (immutable raw versions) ------------------------------
    def add_input(self, project_id, kind, filename, raw_text, meta, items):
        prev = self.latest_input(project_id, kind)
        version = 1 if prev is None else prev["version"] + 1
        parent_id = None if prev is None else prev["input_id"]
        old = {} if prev is None else {i["item_id"]: digest(i) for i in prev["items"]}
        new = {i["item_id"]: digest(i) for i in items}
        changed = sorted(
            [k for k in set(old) | set(new) if old.get(k) != new.get(k)]
        )
        input_id = f"in-{kind}-v{version}"
        seq = self._next_seq(project_id)
        self.conn.execute(
            "INSERT INTO inputs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (input_id, project_id, kind, version, parent_id, filename, raw_text,
             digest(raw_text), _j(meta), _j(items), _j(changed), seq, clock.now()),
        )
        self.conn.commit()
        return self.get_input(input_id)

    def get_input(self, input_id):
        row = self.conn.execute(
            "SELECT * FROM inputs WHERE input_id=?", (input_id,)
        ).fetchone()
        return self._decode(row, ["meta", "items", "changed_items"]) if row else None

    def latest_input(self, project_id, kind, upto_seq=None):
        q = "SELECT * FROM inputs WHERE project_id=? AND kind=?"
        args = [project_id, kind]
        if upto_seq is not None:
            q += " AND seq<=?"
            args.append(upto_seq)
        q += " ORDER BY version DESC LIMIT 1"
        row = self.conn.execute(q, args).fetchone()
        return self._decode(row, ["meta", "items", "changed_items"]) if row else None

    def inputs_between(self, project_id, kind, v_from, v_to, upto_seq=None):
        q = ("SELECT * FROM inputs WHERE project_id=? AND kind=? "
             "AND version>? AND version<=?")
        args = [project_id, kind, v_from, v_to]
        if upto_seq is not None:
            q += " AND seq<=?"
            args.append(upto_seq)
        rows = self.conn.execute(q, args).fetchall()
        return [self._decode(r, ["meta", "items", "changed_items"]) for r in rows]

    def all_inputs(self, project_id):
        rows = self.conn.execute(
            "SELECT * FROM inputs WHERE project_id=? ORDER BY seq", (project_id,)
        ).fetchall()
        return [self._decode(r, ["meta", "items", "changed_items"]) for r in rows]

    # ---- params ---------------------------------------------------------
    def add_params(self, project_id, name, payload, changed_sites):
        prev = self.latest_params(project_id, name)
        version = 1 if prev is None else prev["version"] + 1
        parent_id = None if prev is None else prev["param_id"]
        param_id = f"pa-{name}-v{version}"
        fingerprint = digest(name, payload)[:16]
        seq = self._next_seq(project_id)
        self.conn.execute(
            "INSERT INTO params VALUES(?,?,?,?,?,?,?,?,?)",
            (param_id, project_id, name, version, parent_id, _j(payload),
             fingerprint, _j(changed_sites), seq, clock.now()),
        )
        self.conn.commit()
        return self.get_params(param_id)

    def get_params(self, param_id):
        row = self.conn.execute(
            "SELECT * FROM params WHERE param_id=?", (param_id,)
        ).fetchone()
        return self._decode(row, ["payload", "changed_sites"]) if row else None

    def latest_params(self, project_id, name, upto_seq=None):
        q = "SELECT * FROM params WHERE project_id=? AND name=?"
        args = [project_id, name]
        if upto_seq is not None:
            q += " AND seq<=?"
            args.append(upto_seq)
        q += " ORDER BY version DESC LIMIT 1"
        row = self.conn.execute(q, args).fetchone()
        return self._decode(row, ["payload", "changed_sites"]) if row else None

    def params_between(self, project_id, name, v_from, v_to, upto_seq=None):
        q = ("SELECT * FROM params WHERE project_id=? AND name=? "
             "AND version>? AND version<=?")
        args = [project_id, name, v_from, v_to]
        if upto_seq is not None:
            q += " AND seq<=?"
            args.append(upto_seq)
        rows = self.conn.execute(q, args).fetchall()
        return [self._decode(r, ["payload", "changed_sites"]) for r in rows]

    # ---- events ---------------------------------------------------------
    def add_event(self, project_id, kind, payload, actor="system",
                  branch_id="main", parent_event_id=None):
        seq = self._next_seq(project_id)
        event_id = f"ev-{seq:05d}"
        self.conn.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, project_id, seq, branch_id, kind, actor,
             _j(payload), clock.now(), parent_event_id),
        )
        self.conn.commit()
        return self.get_event(event_id)

    def get_event(self, event_id):
        row = self.conn.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        return self._decode(row, ["payload"]) if row else None

    def events(self, project_id, upto_seq=None, kinds=None):
        q = "SELECT * FROM events WHERE project_id=?"
        args = [project_id]
        if upto_seq is not None:
            q += " AND seq<=?"
            args.append(upto_seq)
        q += " ORDER BY seq"
        rows = self.conn.execute(q, args).fetchall()
        out = [self._decode(r, ["payload"]) for r in rows]
        if kinds:
            out = [e for e in out if e["kind"] in kinds]
        return out

    def branches(self, project_id):
        rows = self.conn.execute(
            "SELECT DISTINCT branch_id FROM events WHERE project_id=? ORDER BY branch_id",
            (project_id,),
        ).fetchall()
        names = [r[0] for r in rows]
        return names or ["main"]

    # ---- derived --------------------------------------------------------
    def add_derived(self, project_id, derived_id, kind, algo_version,
                    fingerprint, deps, payload):
        seq = self._next_seq(project_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO derived VALUES(?,?,?,?,?,?,?,?,?)",
            (derived_id, project_id, kind, algo_version, fingerprint,
             _j(deps), _j(payload), seq, clock.now()),
        )
        self.conn.commit()
        return self.get_derived(derived_id)

    def get_derived(self, derived_id):
        row = self.conn.execute(
            "SELECT * FROM derived WHERE derived_id=?", (derived_id,)
        ).fetchone()
        return self._decode(row, ["deps", "payload"]) if row else None

    def latest_derived(self, project_id, kind, upto_seq=None):
        q = "SELECT * FROM derived WHERE project_id=? AND kind=?"
        args = [project_id, kind]
        if upto_seq is not None:
            q += " AND seq<=?"
            args.append(upto_seq)
        q += " ORDER BY seq DESC LIMIT 1"
        row = self.conn.execute(q, args).fetchone()
        return self._decode(row, ["deps", "payload"]) if row else None

    # ---- export / import --------------------------------------------------
    def export_project(self, project_id):
        data = {"format": "nmr-workbench-export/1", "project_id": project_id,
                "tables": {}}
        for table in TABLES:
            if table == "projects":
                rows = self.conn.execute(
                    "SELECT * FROM projects WHERE project_id=?", (project_id,)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT * FROM {table} WHERE project_id=? ORDER BY seq",
                    (project_id,),
                ).fetchall()
            data["tables"][table] = [dict(r) for r in rows]
        return data

    def import_project(self, data):
        assert data.get("format") == "nmr-workbench-export/1", "bad export format"
        pid = data["project_id"]
        counts = {}
        for table in TABLES:
            rows = data["tables"].get(table, [])
            n = 0
            for row in rows:
                cols = ",".join(row.keys())
                marks = ",".join("?" for _ in row)
                self.conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({marks})",
                    tuple(row.values()),
                )
                n += 1
            counts[table] = n
        self.conn.commit()
        return {"project_id": pid, "imported": counts}

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def _decode(row, json_cols):
        d = dict(row)
        for c in json_cols:
            if c in d and isinstance(d[c], str):
                d[c] = json.loads(d[c])
        return d
