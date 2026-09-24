"""SQLite 事件存储：事件一经写入不可变，所有派生状态由事件折叠而来。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  project   TEXT NOT NULL,
  seq       INTEGER NOT NULL,
  event_id  TEXT NOT NULL UNIQUE,
  kind      TEXT NOT NULL,
  actor     TEXT NOT NULL,
  branch    TEXT NOT NULL,
  parents   TEXT NOT NULL,
  payload   TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(project, seq)
);
"""

EVENT_COLUMNS = ("project", "seq", "event_id", "kind", "actor", "branch", "parents", "payload", "created_at")


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "project": row["project"],
        "seq": row["seq"],
        "event_id": row["event_id"],
        "kind": row["kind"],
        "actor": row["actor"],
        "branch": row["branch"],
        "parents": json.loads(row["parents"]),
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }


class EventStore:
    def __init__(self, db_path, clock):
        self.db_path = str(db_path)
        self.clock = clock
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def append(
        self,
        project: str,
        kind: str,
        payload: dict,
        actor: str = "system",
        branch: str = "main",
        parents: Optional[list[str]] = None,
        seq: Optional[int] = None,
        event_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> dict[str, Any]:
        if seq is None:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE project=?", (project,)
            ).fetchone()
            seq = int(row["n"])
        if event_id is None:
            event_id = f"{project}#e{seq:06d}"
        if created_at is None:
            created_at = self.clock.now()
        parents = list(parents or [])
        self.conn.execute(
            "INSERT INTO events(project,seq,event_id,kind,actor,branch,parents,payload,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (project, seq, event_id, kind, actor, branch, json.dumps(parents, ensure_ascii=False),
             json.dumps(payload, ensure_ascii=False, sort_keys=True), created_at),
        )
        self.conn.commit()
        return {
            "project": project, "seq": seq, "event_id": event_id, "kind": kind,
            "actor": actor, "branch": branch, "parents": parents,
            "payload": payload, "created_at": created_at,
        }

    def insert_row(self, row: dict[str, Any]) -> None:
        """导入归档时原样写入，保留身份、顺序与来源关系。"""
        self.conn.execute(
            "INSERT INTO events(project,seq,event_id,kind,actor,branch,parents,payload,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                row["project"], row["seq"], row["event_id"], row["kind"], row["actor"],
                row["branch"],
                json.dumps(row["parents"], ensure_ascii=False),
                json.dumps(row["payload"], ensure_ascii=False, sort_keys=True),
                row["created_at"],
            ),
        )
        self.conn.commit()

    def events(self, project: str, upto_seq: Optional[int] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE project=?"
        args: list[Any] = [project]
        if upto_seq is not None:
            sql += " AND seq<=?"
            args.append(upto_seq)
        sql += " ORDER BY seq"
        return [_row_to_event(r) for r in self.conn.execute(sql, args).fetchall()]

    def get(self, event_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None

    def projects(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT project FROM events ORDER BY project").fetchall()
        return [r["project"] for r in rows]

    def is_ancestor(self, project: str, ancestor_id: str, event_id: str) -> bool:
        """ancestor_id 是否为 event_id 的前驱（沿 parents 链）。"""
        seen: set[str] = set()
        stack = [event_id]
        while stack:
            current = stack.pop()
            if current == ancestor_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            rec = self.get(current)
            if rec is None or rec["project"] != project:
                continue
            stack.extend(rec["parents"])
        return False
