"""Append-only event store on SQLite.

Raw inputs are immutable once submitted; revisions are new versions with
a parent-version link. Every derived value records algorithm version,
parameter fingerprint and direct source event ids, so a project can be
replayed step by step after reopening.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
"""


class EventStore:
    def __init__(self, path: str | Path, clock: Optional[Callable[[], float]] = None):
        self.path = str(path)
        self.clock = clock or time.time
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def append(self, event_id: str, kind: str, payload: dict,
               branch_id: str = "main", actor: str = "system") -> dict:
        created = self.clock()
        self.conn.execute(
            "INSERT INTO events(event_id, kind, branch_id, actor, payload, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (event_id, kind, branch_id, actor, json.dumps(payload, sort_keys=True), created),
        )
        self.conn.commit()
        return self.get_by_event_id(event_id)

    def get_by_event_id(self, event_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return _row_to_event(row)

    def all(self, upto_seq: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM events"
        args: tuple = ()
        if upto_seq is not None:
            sql += " WHERE seq<=?"
            args = (upto_seq,)
        sql += " ORDER BY seq"
        return [_row_to_event(r) for r in self.conn.execute(sql, args).fetchall()]

    def of_kind(self, kind: str, upto_seq: Optional[int] = None) -> list[dict]:
        return [e for e in self.all(upto_seq) if e["kind"] == kind]

    def export_events(self) -> list[dict]:
        return self.all()

    def import_events(self, events: Iterable[dict]) -> int:
        """Import an export preserving identity (event_id), order (seq
        re-derived from given order) and provenance (payload untouched)."""
        n = 0
        for e in events:
            self.conn.execute(
                "INSERT OR IGNORE INTO events(event_id, kind, branch_id, actor, payload, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (e["event_id"], e["kind"], e["branch_id"], e["actor"],
                 json.dumps(e["payload"], sort_keys=True), e["created_at"]),
            )
            n += 1
        self.conn.commit()
        return n

    def close(self) -> None:
        self.conn.close()


def _row_to_event(row: sqlite3.Row) -> dict:
    return {
        "seq": row["seq"],
        "event_id": row["event_id"],
        "kind": row["kind"],
        "branch_id": row["branch_id"],
        "actor": row["actor"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }
