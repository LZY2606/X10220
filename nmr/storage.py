from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .clock import iso_now


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  seq INTEGER NOT NULL,
  parent_event_ids TEXT NOT NULL,
  branch_name TEXT NOT NULL,
  editor_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, seq)
);
CREATE INDEX IF NOT EXISTS events_project ON events(project_id, seq);
"""


class Store:
    def __init__(self, path: str | os.PathLike[str] = "data/nmr.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def create_project(self, name: str, project_id: str | None = None, created_at: str | None = None) -> str:
        final_id = project_id or _stable_id("project", name)
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO projects(project_id,name,created_at) VALUES(?,?,?)",
                (final_id, name, created_at or iso_now()),
            )
        return final_id

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at, project_id").fetchall()
        return [dict(row) for row in rows]

    def append_event(
        self,
        project_id: str,
        event_type: str,
        payload: dict[str, Any],
        parent_event_ids: list[str] | None = None,
        branch_name: str = "main",
        editor_id: str = "reviewer",
        created_at: str | None = None,
        event_id: str | None = None,
    ) -> str:
        parents = parent_event_ids or self.head_ids(project_id, branch_name)
        with self.connect() as conn:
            seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM events WHERE project_id=?", (project_id,)).fetchone()["next_seq"]
            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            final_id = event_id or _stable_id("event", project_id, str(seq), event_type, payload_json)
            timestamp = created_at or iso_now()
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (final_id, project_id, seq, json.dumps(parents), branch_name, editor_id, event_type, payload_json, timestamp),
            )
        return final_id

    def head_ids(self, project_id: str, branch_name: str) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT event_id, branch_name FROM events WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()
        if not rows:
            return []
        all_ids = {row["event_id"] for row in rows}
        referenced = set()
        for row in rows:
            referenced.update(json.loads(self._payload_parent(row)))
        heads = all_ids - referenced
        branch_rows = [row for row in rows if row["event_id"] in heads]
        selected = [row["event_id"] for row in branch_rows if row["branch_name"] == branch_name]
        return selected or [sorted(heads)[0]] if heads else []

    @staticmethod
    def _payload_parent(row: sqlite3.Row) -> str:
        return row["parent_event_ids"]

    def list_events(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()
        return [_event_from_row(row) for row in rows]

    def get_event(self, event_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return _event_from_row(row)

    def replace_with_export(self, export_data: dict[str, Any]) -> None:
        project = export_data["project"]
        events = export_data["events"]
        with self.connect() as conn:
            conn.execute("DELETE FROM projects")
            conn.execute("DELETE FROM events")
            conn.execute("INSERT INTO projects VALUES(?,?,?)", (project["project_id"], project["name"], project["created_at"]))
            for event in events:
                conn.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        event["event_id"],
                        project["project_id"],
                        event["seq"],
                        json.dumps(event["parent_event_ids"]),
                        event["branch_name"],
                        event["editor_id"],
                        event["event_type"],
                        json.dumps(event["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        event["created_at"],
                    ),
                )

    def export_project(self, project_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            project_row = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if project_row is None:
                raise KeyError(project_id)
            rows = conn.execute("SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()
        return {"format": "nmr-evidence-archive/1", "project": dict(project_row), "events": [_event_from_row(row) for row in rows]}


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "project_id": row["project_id"],
        "seq": row["seq"],
        "parent_event_ids": json.loads(row["parent_event_ids"]),
        "branch_name": row["branch_name"],
        "editor_id": row["editor_id"],
        "event_type": row["event_type"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\u0000".join(parts).encode("utf-8")).hexdigest()[:16]
