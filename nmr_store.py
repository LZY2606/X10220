from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = "nmr-candidate-archive/1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(value: Any) -> str:
    return sha256_text(canonical_json(value))


class ConcurrencyError(ValueError):
    pass


class EventStore:
    def __init__(
        self,
        path: str | Path = ":memory:",
        clock: Callable[[], str] = utc_now,
        id_factory: Callable[[], str] | None = None,
    ):
        self.path = str(path)
        self.clock = clock
        self.id_factory = id_factory or (lambda: "evt_" + uuid.uuid4().hex)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                parent_event_id TEXT,
                payload TEXT NOT NULL,
                UNIQUE(project_id, seq),
                FOREIGN KEY(parent_event_id) REFERENCES events(event_id)
            )
            """
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS events_project ON events(project_id, seq)")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_branch ON events(branch_id, seq)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def event_exists(self, event_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone()
        return row is not None

    def append(
        self,
        project_id: str,
        branch_id: str,
        event_type: str,
        payload: dict[str, Any],
        parent_event_id: str | None = None,
        event_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        event_id = event_id or self.id_factory()
        if self.event_exists(event_id):
            raise ConcurrencyError(f"event already exists: {event_id}")
        if parent_event_id and not self.event_exists(parent_event_id):
            raise ConcurrencyError(f"unknown parent event: {parent_event_id}")
        timestamp = timestamp or self.clock()
        payload = dict(payload)
        payload.setdefault("event_id", event_id)
        try:
            seq_row = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM events WHERE project_id=?",
                (project_id,),
            ).fetchone()
            seq = int(seq_row["seq"])
            self.db.execute(
                """
                INSERT INTO events(event_id, project_id, branch_id, seq, timestamp,
                                   event_type, parent_event_id, payload)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    project_id,
                    branch_id,
                    seq,
                    timestamp,
                    event_type,
                    parent_event_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            raise ConcurrencyError(str(exc)) from exc
        return self.get_event(event_id)

    def append_many(self, events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = list(events)
        result: list[dict[str, Any]] = []
        try:
            for raw in prepared:
                seq_row = self.db.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM events WHERE project_id=?",
                    (raw["project_id"],),
                ).fetchone()
                event_id = raw.get("event_id") or self.id_factory()
                payload = dict(raw["payload"])
                payload["event_id"] = event_id
                self.db.execute(
                    """
                    INSERT INTO events(event_id, project_id, branch_id, seq, timestamp,
                                       event_type, parent_event_id, payload)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        event_id,
                        raw["project_id"],
                        raw["branch_id"],
                        int(seq_row["seq"]),
                        raw.get("timestamp") or self.clock(),
                        raw["event_type"],
                        raw.get("parent_event_id"),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                result.append(
                    {
                        "event_id": event_id,
                        "project_id": raw["project_id"],
                        "branch_id": raw["branch_id"],
                        "seq": int(seq_row["seq"]),
                        "timestamp": raw.get("timestamp") or self.clock(),
                        "event_type": raw["event_type"],
                        "parent_event_id": raw.get("parent_event_id"),
                        "payload": payload,
                    }
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return result

    def get_event(self, event_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._row_to_event(row)

    def list_events(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,)
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def list_projects(self) -> list[dict[str, str]]:
        rows = self.db.execute(
            """
            SELECT project_id, MIN(timestamp) AS created_at
            FROM events GROUP BY project_id ORDER BY MIN(seq)
            """
        ).fetchall()
        return [{"project_id": row["project_id"], "created_at": row["created_at"]} for row in rows]

    def ancestors(self, event_id: str) -> list[dict[str, Any]]:
        by_id = {event["event_id"]: event for event in self.list_events(self.get_event(event_id)["project_id"])}
        if event_id not in by_id:
            raise KeyError(event_id)
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        current: str | None = event_id
        while current:
            if current in seen:
                raise ConcurrencyError(f"event cycle at {current}")
            seen.add(current)
            event = by_id[current]
            chain.append(event)
            current = event.get("parent_event_id")
        return sorted(chain, key=lambda event: event["seq"])

    def export_project(self, project_id: str) -> dict[str, Any]:
        events = self.list_events(project_id)
        return {
            "archive_format": SCHEMA_VERSION,
            "project_id": project_id,
            "exported_at": self.clock(),
            "events": events,
        }

    def import_archive(self, archive: dict[str, Any], verify_project_id: str | None = None) -> str:
        if archive.get("archive_format") != SCHEMA_VERSION:
            raise ValueError("unsupported archive format")
        events = archive.get("events")
        if not isinstance(events, list) or not events:
            raise ValueError("archive contains no events")
        project_id = archive.get("project_id")
        if not project_id:
            raise ValueError("archive has no project_id")
        if verify_project_id and project_id != verify_project_id:
            raise ValueError("project_id mismatch")
        known = {event["event_id"] for event in events}
        for event in events:
            if event.get("parent_event_id") and event["parent_event_id"] not in known:
                raise ValueError(f"missing parent {event['parent_event_id']}")
        return project_id, self.append_many(
            {
                "event_id": event["event_id"],
                "project_id": event["project_id"],
                "branch_id": event["branch_id"],
                "timestamp": event["timestamp"],
                "event_type": event["event_type"],
                "parent_event_id": event.get("parent_event_id"),
                "payload": event["payload"],
            }
            for event in events
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "project_id": row["project_id"],
            "branch_id": row["branch_id"],
            "seq": int(row["seq"]),
            "timestamp": row["timestamp"],
            "event_type": row["event_type"],
            "parent_event_id": row["parent_event_id"],
            "payload": json.loads(row["payload"]),
        }
