"""Append-only event log and typed event definitions.

The event log is the system of record. Projects are a linear list of events;
branches are views over an event id with a parent pointer. Nothing is ever
overwritten: retractions, conflicts and resolutions are themselves events.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .clock import now_iso
from .ids import content_hash, short_hash


EVENT_TYPES = {
    "project.created",
    "blob.committed",
    "source.versioned",
    "param.revised",
    "analysis.ran",
    "decision.recorded",
    "decision.retracted",
    "branch.created",
    "merge.conflicted",
    "merge.resolved",
}


@dataclass
class Event:
    seq: int
    id: str
    project_id: str
    branch_id: str
    type: str
    author: str
    created_at: str
    payload: dict[str, Any]
    parents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "id": self.id,
            "project_id": self.project_id,
            "branch_id": self.branch_id,
            "type": self.type,
            "author": self.author,
            "created_at": self.created_at,
            "payload": self.payload,
            "parents": self.parents,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        return cls(
            seq=row["seq"],
            id=row["id"],
            project_id=row["project_id"],
            branch_id=row["branch_id"],
            type=row["type"],
            author=row["author"],
            created_at=row["created_at"],
            payload=json.loads(row["payload"]),
            parents=json.loads(row["parents"]),
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    head_event_id TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    type TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    parents TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS events_project ON events(project_id, seq);
CREATE TABLE IF NOT EXISTS blobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    raw TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kvmeta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class EventStore:
    """SQLite-backed append-only log + content-addressed blob store."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- meta -----------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kvmeta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kvmeta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- projects -------------------------------------------------------
    def create_project(self, project_id: str, title: str, created_at: str) -> None:
        self.conn.execute(
            "INSERT INTO projects(id, created_at, title) VALUES(?,?,?)",
            (project_id, created_at, title),
        )
        self.conn.commit()

    def list_projects(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM projects ORDER BY created_at, id"))

    def get_project(self, project_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()

    def project_exists(self, project_id: str) -> bool:
        return self.get_project(project_id) is not None

    # -- blobs ----------------------------------------------------------
    def put_blob(
        self,
        blob_id: str,
        project_id: str,
        filename: str,
        media_type: str,
        raw: str,
        sha256: str,
    ) -> str:
        existing = self.conn.execute(
            "SELECT id FROM blobs WHERE id=?", (blob_id,)
        ).fetchone()
        if existing:
            return existing["id"]
        self.conn.execute(
            "INSERT INTO blobs(id,project_id,filename,media_type,raw,size,sha256)"
            " VALUES(?,?,?,?,?,?,?)",
            (blob_id, project_id, filename, media_type, raw, len(raw.encode()), sha256),
        )
        self.conn.commit()
        return blob_id

    def get_blob(self, blob_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM blobs WHERE id=?", (blob_id,)).fetchone()

    def list_blobs(self, project_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM blobs WHERE project_id=? ORDER BY id", (project_id,)
            )
        )

    # -- events ---------------------------------------------------------
    def append(
        self,
        project_id: str,
        branch_id: str,
        type_: str,
        payload: dict[str, Any],
        author: str = "system",
        parents: list[str] | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type_}")
        payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        parents = parents or []
        created_at = created_at or now_iso()
        # Deterministic id from content; dedupe replays of identical commits.
        eid = event_id or "evt_" + short_hash(
            [project_id, branch_id, type_, payload_str, created_at, author, parents]
        )
        existing = self.conn.execute(
            "SELECT * FROM events WHERE id=?", (eid,)
        ).fetchone()
        if existing:
            return Event.from_row(existing)
        cur = self.conn.execute(
            "INSERT INTO events(id,project_id,branch_id,type,author,created_at,"
            "payload,parents) VALUES(?,?,?,?,?,?,?,?)",
            (
                eid,
                project_id,
                branch_id,
                type_,
                author,
                created_at,
                payload_str,
                json.dumps(parents, sort_keys=True),
            ),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM events WHERE seq=?", (cur.lastrowid,)).fetchone()
        return Event.from_row(row)

    def events_for(self, project_id: str) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,)
        ).fetchall()
        return [Event.from_row(r) for r in rows]

    def next_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM events").fetchone()
        return int(row["n"])

    # -- backup / restore ----------------------------------------------
    def dump(self) -> dict[str, Any]:
        projects = [dict(r) for r in self.conn.execute("SELECT * FROM projects")]
        events = [
            Event.from_row(r).to_dict()
            for r in self.conn.execute("SELECT * FROM events ORDER BY seq")
        ]
        blobs = [dict(r) for r in self.conn.execute("SELECT * FROM blobs ORDER BY id")]
        kv = [dict(r) for r in self.conn.execute("SELECT * FROM kvmeta")]
        return {"projects": projects, "events": events, "blobs": blobs, "kvmeta": kv}

    def load_dump(self, dump: dict[str, Any]) -> None:
        with self.conn:
            for p in dump.get("projects", []):
                self.conn.execute(
                    "INSERT OR REPLACE INTO projects(id,created_at,title,head_event_id)"
                    " VALUES(?,?,?,?)",
                    (p["id"], p["created_at"], p["title"], p.get("head_event_id")),
                )
            for b in dump.get("blobs", []):
                self.conn.execute(
                    "INSERT OR REPLACE INTO blobs(id,project_id,filename,media_type,"
                    "raw,size,sha256) VALUES(?,?,?,?,?,?,?)",
                    (
                        b["id"],
                        b["project_id"],
                        b["filename"],
                        b["media_type"],
                        b["raw"],
                        b["size"],
                        b["sha256"],
                    ),
                )
            for e in dump.get("events", []):
                self.conn.execute(
                    "INSERT OR REPLACE INTO events(seq,id,project_id,branch_id,type,"
                    "author,created_at,payload,parents) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        e["seq"],
                        e["id"],
                        e["project_id"],
                        e["branch_id"],
                        e["type"],
                        e["author"],
                        e["created_at"],
                        json.dumps(e["payload"], sort_keys=True, ensure_ascii=False),
                        json.dumps(e["parents"], sort_keys=True),
                    ),
                )
            for kv in dump.get("kvmeta", []):
                self.conn.execute(
                    "INSERT OR REPLACE INTO kvmeta(key,value) VALUES(?,?)",
                    (kv["key"], kv["value"]),
                )


def hash_payload(payload: dict[str, Any]) -> str:
    return content_hash(payload)
