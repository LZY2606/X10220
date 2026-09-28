"""SQLite-backed append-only event store with export/import round-trip."""
from __future__ import annotations

import json
import sqlite3

from .events import EVENT_KINDS, ReducerError
from .ident import canonical_json, content_sha

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    branch TEXT NOT NULL,
    editor TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    parent_event_id TEXT,
    merge_parents TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(project_id, seq)
);
CREATE TABLE IF NOT EXISTS archives (
    archive_sha TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class EventStore:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def list_projects(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT project_id, title, created_at FROM projects ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def project_exists(self, project_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is not None

    def create_project(self, project_id: str, title: str, created_at: str) -> None:
        self.conn.execute(
            "INSERT INTO projects(project_id, title, created_at) VALUES (?, ?, ?)",
            (project_id, title, created_at))
        self.conn.commit()

    def next_seq(self, project_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE project_id = ?",
            (project_id,)).fetchone()
        return int(row["m"]) + 1

    def append(self, project_id: str, event: dict) -> None:
        if event["kind"] not in EVENT_KINDS:
            raise ReducerError(f"unknown event kind {event['kind']!r}")
        self.conn.execute(
            """INSERT INTO events(id, project_id, seq, kind, branch, editor, timestamp,
                                  parent_event_id, merge_parents, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event["id"], project_id, event["seq"], event["kind"], event["branch"],
             event["editor"], event["timestamp"], event["parent_event_id"],
             json.dumps(event["merge_parents"]), json.dumps(event["payload"],
                                                            ensure_ascii=False)))
        self.conn.commit()

    def load_events(self, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE project_id = ? ORDER BY seq", (project_id,)).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "seq": row["seq"],
            "branch": row["branch"],
            "editor": row["editor"],
            "timestamp": row["timestamp"],
            "parent_event_id": row["parent_event_id"],
            "merge_parents": json.loads(row["merge_parents"]),
            "payload": json.loads(row["payload"]),
        }

    def export_archive(self, project_id: str, created_at: str) -> dict:
        project = self.conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        if project is None:
            raise ReducerError(f"unknown project {project_id!r}")
        archive = {
            "format": "nmr-workbench-archive/1",
            "project": {"project_id": project["project_id"], "title": project["title"],
                        "created_at": project["created_at"]},
            "events": self.load_events(project_id),
            "exported_at": created_at,
        }
        archive["archive_sha"] = content_sha(canonical_json(
            {k: v for k, v in archive.items() if k != "archive_sha"}))
        return archive

    def import_archive(self, archive: dict) -> str:
        if archive.get("format") != "nmr-workbench-archive/1":
            raise ReducerError("unsupported archive format")
        expected = archive.get("archive_sha")
        actual = content_sha(canonical_json(
            {k: v for k, v in archive.items() if k != "archive_sha"}))
        if expected != actual:
            raise ReducerError("archive checksum mismatch; refusing import")
        project = archive["project"]
        if not self.project_exists(project["project_id"]):
            self.create_project(project["project_id"], project["title"],
                                project["created_at"])
        for event in sorted(archive["events"], key=lambda e: e["seq"]):
            if self.conn.execute(
                    "SELECT 1 FROM events WHERE id = ?", (event["id"],)).fetchone():
                continue
            self.append(project["project_id"], event)
        self.conn.commit()
        self.conn.execute(
            "INSERT OR IGNORE INTO archives(archive_sha, project_id, created_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (actual, project["project_id"], archive["exported_at"],
             canonical_json(archive)))
        self.conn.commit()
        return project["project_id"]

    def delete_project(self, project_id: str) -> None:
        self.conn.execute("DELETE FROM events WHERE project_id = ?", (project_id,))
        self.conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        self.conn.commit()
