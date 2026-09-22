"""SQLite persistence: append-only event log, immutable raw inputs, derived cache.

Everything is stored in one database file.  Events carry an explicit ``seq``
so that export -> delete -> import preserves identity and order exactly.
"""
from __future__ import annotations

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY,
  event_id TEXT UNIQUE NOT NULL,
  branch TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  ts TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
  name TEXT PRIMARY KEY,
  parent TEXT,
  fork_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS raw_inputs (
  doc_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  parent_version INTEGER,
  content TEXT NOT NULL,
  fmt TEXT NOT NULL,
  event_id TEXT NOT NULL,
  PRIMARY KEY (doc_id, version)
);
CREATE TABLE IF NOT EXISTS derived (
  derived_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  algo_version TEXT NOT NULL,
  param_fp TEXT NOT NULL,
  sources TEXT NOT NULL,
  payload TEXT NOT NULL,
  event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- events ---------------------------------------------------------
    def next_seq(self):
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events").fetchone()
        return int(row["n"])

    def append_event(self, event_id, branch, kind, actor, ts, payload, seq=None):
        with self.lock:
            if seq is None:
                seq = self.next_seq()
            self.conn.execute(
                "INSERT INTO events(seq, event_id, branch, kind, actor, ts, payload)"
                " VALUES (?,?,?,?,?,?,?)",
                (seq, event_id, branch, kind, actor, ts, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
            self.conn.commit()
        return seq

    def events(self):
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [self._event(r) for r in rows]

    def event(self, event_id):
        row = self.conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return self._event(row) if row else None

    @staticmethod
    def _event(row):
        return {
            "seq": row["seq"],
            "event_id": row["event_id"],
            "branch": row["branch"],
            "kind": row["kind"],
            "actor": row["actor"],
            "ts": row["ts"],
            "payload": json.loads(row["payload"]),
        }

    # ---- branches -------------------------------------------------------
    def add_branch(self, name, parent, fork_seq):
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO branches(name, parent, fork_seq) VALUES (?,?,?)",
                (name, parent, fork_seq),
            )
            self.conn.commit()

    def branches(self):
        rows = self.conn.execute("SELECT * FROM branches ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    # ---- raw inputs (immutable) ----------------------------------------
    def put_raw(self, doc_id, version, parent_version, content, fmt, event_id):
        with self.lock:
            self.conn.execute(
                "INSERT INTO raw_inputs(doc_id, version, parent_version, content, fmt, event_id)"
                " VALUES (?,?,?,?,?,?)",
                (doc_id, version, parent_version, content, fmt, event_id),
            )
            self.conn.commit()

    def raw_inputs(self):
        rows = self.conn.execute(
            "SELECT * FROM raw_inputs ORDER BY doc_id, version"
        ).fetchall()
        return [dict(r) for r in rows]

    def raw(self, doc_id, version):
        row = self.conn.execute(
            "SELECT * FROM raw_inputs WHERE doc_id=? AND version=?", (doc_id, version)
        ).fetchone()
        return dict(row) if row else None

    # ---- derived cache --------------------------------------------------
    def put_derived(self, derived_id, kind, algo_version, param_fp, sources, payload, event_id):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO derived(derived_id, kind, algo_version, param_fp, sources, payload, event_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (derived_id, kind, algo_version, param_fp, json.dumps(sources, ensure_ascii=False),
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), event_id),
            )
            self.conn.commit()

    def derived(self):
        rows = self.conn.execute("SELECT * FROM derived ORDER BY derived_id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sources"] = json.loads(d["sources"])
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    # ---- kv -------------------------------------------------------------
    def get_kv(self, key, default=None):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_kv(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?,?)", (key, value))
            self.conn.commit()

    # ---- bulk ------------------------------------------------------------
    def is_empty(self):
        row = self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return row["n"] == 0

    def wipe(self):
        with self.lock:
            for table in ("events", "branches", "raw_inputs", "derived", "kv"):
                self.conn.execute("DELETE FROM %s" % table)
            self.conn.commit()

    def export_bundle(self):
        return {
            "format": "nmr-gsb-export",
            "version": 1,
            "events": self.events(),
            "branches": self.branches(),
            "raw_inputs": self.raw_inputs(),
            "derived": self.derived(),
            "kv": {r["k"]: r["v"] for r in self.conn.execute("SELECT * FROM kv").fetchall()},
        }

    def import_bundle(self, bundle):
        if bundle.get("format") != "nmr-gsb-export":
            raise ValueError("not an nmr-gsb-export bundle")
        if not self.is_empty():
            raise ValueError("import target database is not empty")
        with self.lock:
            for b in bundle["branches"]:
                self.conn.execute(
                    "INSERT INTO branches(name, parent, fork_seq) VALUES (?,?,?)",
                    (b["name"], b["parent"], b["fork_seq"]),
                )
            for e in bundle["events"]:
                self.conn.execute(
                    "INSERT INTO events(seq, event_id, branch, kind, actor, ts, payload)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (e["seq"], e["event_id"], e["branch"], e["kind"], e["actor"], e["ts"],
                     json.dumps(e["payload"], ensure_ascii=False, sort_keys=True)),
                )
            for r in bundle["raw_inputs"]:
                self.conn.execute(
                    "INSERT INTO raw_inputs(doc_id, version, parent_version, content, fmt, event_id)"
                    " VALUES (?,?,?,?,?,?)",
                    (r["doc_id"], r["version"], r["parent_version"], r["content"], r["fmt"], r["event_id"]),
                )
            for d in bundle["derived"]:
                self.conn.execute(
                    "INSERT INTO derived(derived_id, kind, algo_version, param_fp, sources, payload, event_id)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (d["derived_id"], d["kind"], d["algo_version"], d["param_fp"],
                     json.dumps(d["sources"], ensure_ascii=False),
                     json.dumps(d["payload"], ensure_ascii=False, sort_keys=True), d["event_id"]),
                )
            for k, v in bundle.get("kv", {}).items():
                self.conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?,?)", (k, v))
            self.conn.commit()
