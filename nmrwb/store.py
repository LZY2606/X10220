"""SQLite event-sourced store.

Every object carries a stable identity (deterministic per-project counters),
raw inputs are immutable and versioned through parent links, derived values
record algorithm version + parameter fingerprint + direct sources, and all
judgements are machine-readable events. Export/import preserves ids, order
(seq) and provenance verbatim."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, name TEXT, meta_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS counters(
  project_id TEXT, name TEXT, value INTEGER,
  PRIMARY KEY(project_id, name));
CREATE TABLE IF NOT EXISTS branches(
  id TEXT PRIMARY KEY, project_id TEXT, name TEXT,
  parent_branch_id TEXT, fork_seq INTEGER, created_seq INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS inputs(
  id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, version INTEGER,
  parent_id TEXT, raw_json TEXT, submitted_by TEXT, created_seq INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS input_fragments(
  input_id TEXT, fragment_key TEXT, content_json TEXT,
  PRIMARY KEY(input_id, fragment_key));
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, project_id TEXT, seq INTEGER, branch_id TEXT,
  type TEXT, actor TEXT, payload_json TEXT, parents_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS derived(
  id TEXT PRIMARY KEY, project_id TEXT, branch_id TEXT, kind TEXT,
  algo_version TEXT, param_fp TEXT, sources_json TEXT, status TEXT,
  payload_json TEXT, created_seq INTEGER, stale_since_seq INTEGER);
CREATE TABLE IF NOT EXISTS diagnostics(
  id TEXT PRIMARY KEY, project_id TEXT, input_id TEXT, fragment_key TEXT,
  message TEXT, severity TEXT, created_seq INTEGER);
"""

TABLES = ["projects", "counters", "branches", "inputs", "input_fragments",
          "events", "derived", "diagnostics"]


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock, self.conn:
            self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    # -- identity -----------------------------------------------------------
    def next_seq(self, project_id: str) -> int:
        return int(self.next_id(project_id, "seq", "").split("_")[-1])

    def next_id(self, project_id: str, name: str, prefix: str) -> str:
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT value FROM counters WHERE project_id=? AND name=?",
                (project_id, name)).fetchone()
            value = 1 if row is None else int(row["value"]) + 1
            self.conn.execute(
                "INSERT INTO counters(project_id,name,value) VALUES(?,?,?) "
                "ON CONFLICT(project_id,name) DO UPDATE SET value=excluded.value",
                (project_id, name, value))
        return f"{prefix}_{value:06d}" if prefix else str(value)

    # -- generic helpers ----------------------------------------------------
    def insert(self, table: str, row: dict[str, Any]):
        cols = ",".join(row.keys())
        qs = ",".join("?" for _ in row)
        with self.lock, self.conn:
            self.conn.execute(f"INSERT INTO {table}({cols}) VALUES({qs})",
                              tuple(row.values()))

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args: tuple = ()):
        with self.lock, self.conn:
            self.conn.execute(sql, args)

    # -- export / import ----------------------------------------------------
    def export_project(self, project_id: str) -> dict:
        data: dict[str, Any] = {"format": "nmrwb-export/1", "project_id": project_id,
                                "tables": {}}
        for table in TABLES:
            if table == "input_fragments":
                rows = self.query(
                    "SELECT f.* FROM input_fragments f JOIN inputs i ON f.input_id=i.id "
                    "WHERE i.project_id=? ORDER BY f.input_id, f.fragment_key", (project_id,))
            elif table == "counters":
                rows = self.query("SELECT * FROM counters WHERE project_id=? ORDER BY name",
                                  (project_id,))
            elif table == "events":
                rows = self.query("SELECT * FROM events WHERE project_id=? ORDER BY seq",
                                  (project_id,))
            elif table == "projects":
                rows = self.query("SELECT * FROM projects WHERE id=?", (project_id,))
            else:
                rows = self.query(
                    f"SELECT * FROM {table} WHERE project_id=? ORDER BY rowid", (project_id,))
            data["tables"][table] = rows
        return data

    def import_project(self, data: dict) -> str:
        assert data.get("format") == "nmrwb-export/1", "unsupported export format"
        project_id = data["project_id"]
        with self.lock, self.conn:
            for table in TABLES:
                for row in data["tables"].get(table, []):
                    cols = ",".join(row.keys())
                    qs = ",".join("?" for _ in row)
                    self.conn.execute(f"INSERT INTO {table}({cols}) VALUES({qs})",
                                      tuple(row.values()))
        return project_id


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: str):
    return json.loads(text)
