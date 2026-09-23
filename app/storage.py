from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, path: str | Path, clock: Callable[[], str] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL UNIQUE,
                    branch_id TEXT NOT NULL,
                    parent_event_id TEXT,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS branches (
                    branch_id TEXT PRIMARY KEY,
                    parent_branch_id TEXT,
                    fork_event_id TEXT,
                    label TEXT NOT NULL,
                    created_event_id TEXT NOT NULL,
                    head_event_id TEXT,
                    archived INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def now(self) -> str:
        if self.clock is None:
            from datetime import datetime, timezone

            return datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self.clock()

    def ensure_branch(self, branch_id: str = "main") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO branches(branch_id, label, created_event_id, head_event_id) VALUES (?, ?, ?, NULL)",
                (branch_id, "main" if branch_id == "main" else branch_id, f"branch:{branch_id}"),
            )

    def append(
        self,
        kind: str,
        payload: dict[str,Any],
        branch_id: str = "main",
        parent_event_id: str | None = None,
        event_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_branch(branch_id)
        with self.connect() as conn:
            if parent_event_id is None:
                row = conn.execute(
                    "SELECT event_id FROM events WHERE branch_id = ? ORDER BY seq DESC LIMIT 1",
                    (branch_id,),
                ).fetchone()
                parent_event_id = row["event_id"] if row else None
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
            seq = int(row["m"]) + 1
            event_id = event_id or f"e{seq:06d}"
            stored_payload = dict(payload)
            stored_payload.setdefault("event_id", event_id)
            stored_payload.setdefault("seq", seq)
            stored_payload.setdefault("branch_id", branch_id)
            h = content_hash(stored_payload)
            ts = timestamp or self.now()
            conn.execute(
                "INSERT INTO events(event_id, seq, branch_id, parent_event_id, kind, payload, timestamp, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    seq,
                    branch_id,
                    parent_event_id,
                    kind,
                    json.dumps(stored_payload, ensure_ascii=False, sort_keys=True),
                    ts,
                    h,
                ),
            )
            conn.execute(
                "UPDATE branches SET head_event_id = ? WHERE branch_id = ?",
                (event_id, branch_id),
            )
            return self.event_dict(conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone())

    def create_branch(self, branch_id: str, label: str, parent_branch_id: str, fork_event_id: str, created_event_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO branches(branch_id, parent_branch_id, fork_event_id, label, created_event_id, head_event_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (branch_id, parent_branch_id, fork_event_id, label, created_event_id, fork_event_id),
            )

    @staticmethod
    def event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "seq": row["seq"],
            "branch_id": row["branch_id"],
            "parent_event_id": row["parent_event_id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "timestamp": row["timestamp"],
            "hash": row["hash"],
        }

    def events(self, branch_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if branch_id is None:
                rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
            else:
                rows = conn.execute("SELECT * FROM events WHERE branch_id = ? ORDER BY seq", (branch_id,)).fetchall()
            return [self.event_dict(row) for row in rows]

    def branches(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM branches ORDER BY branch_id").fetchall()
            return [dict(row) for row in rows]

    def export_bundle(self) -> dict[str, Any]:
        with self.connect() as conn:
            events = [self.event_dict(row) for row in conn.execute("SELECT * FROM events ORDER BY seq")]
            branches = [dict(row) for row in conn.execute("SELECT * FROM branches ORDER BY branch_id")]
        return {"schema_version": SCHEMA_VERSION, "branches": branches, "events": events}

    def replace_with_bundle(self, bundle: dict[str, Any]) -> None:
        events = bundle.get("events", [])
        branches = bundle.get("branches", [])
        if not isinstance(events, list) or not isinstance(branches, list):
            raise ValueError("bundle must contain events and branches lists")
        with self.connect() as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM branches")
            for branch in branches:
                conn.execute(
                    """INSERT INTO branches(branch_id, parent_branch_id, fork_event_id, label, created_event_id, head_event_id, archived)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        branch["branch_id"],
                        branch.get("parent_branch_id"),
                        branch.get("fork_event_id"),
                        branch.get("label", branch["branch_id"]),
                        branch["created_event_id"],
                        branch.get("head_event_id"),
                        int(branch.get("archived", 0)),
                    ),
                )
            for event in events:
                payload = event["payload"]
                h = content_hash(payload)
                if event.get("hash") not in (None, h):
                    raise ValueError(f"event hash mismatch: {event[event_id]}")
                conn.execute(
                    "INSERT INTO events(event_id, seq, branch_id, parent_event_id, kind, payload, timestamp, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event["event_id"],
                        event["seq"],
                        event["branch_id"],
                        event.get("parent_event_id"),
                        event["kind"],
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        event["timestamp"],
                        h,
                    ),
                )

