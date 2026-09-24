"""SQLite 追加式事件日志。

- event_id 为内容哈希（prev_id + 规范化负载），导入/导出后保持稳定。
- 原始输入一经提交不可变：source_committed 只新增版本与父版本关系。
- 分支为事件序列上的指针；冲突解决把旧决定引用进新分支。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    branch TEXT NOT NULL,
    parent_event_id TEXT,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    head_event_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, name)
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

CANONICAL_KW = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": False}


def canonical(payload: dict) -> str:
    return json.dumps(payload, **CANONICAL_KW)


def event_content_hash(prev_id: str | None, payload: dict) -> str:
    basis = (prev_id or "ROOT") + "|" + canonical(payload)
    return "evt:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


class StoreError(RuntimeError):
    pass


class EventStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- projects / branches ----
    def register_project(self, project_id: str, name: str, created_at: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO projects(project_id,name,created_at) VALUES(?,?,?)",
            (project_id, name, created_at))
        self.conn.commit()

    def list_projects(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM projects ORDER BY created_at").fetchall()]

    def create_branch(self, project_id: str, name: str, head_event_id: str | None,
                      created_at: str) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM branches WHERE project_id=? AND name=?",
            (project_id, name)).fetchone()
        if exists:
            raise StoreError(f"branch already exists: {name}")
        self.conn.execute(
            "INSERT INTO branches(project_id,name,head_event_id,created_at) VALUES(?,?,?,?)",
            (project_id, name, head_event_id, created_at))
        self.conn.commit()

    def branches(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM branches WHERE project_id=? ORDER BY rowid",
            (project_id,)).fetchall()]

    def head(self, project_id: str, branch: str) -> str | None:
        row = self.conn.execute(
            "SELECT head_event_id FROM branches WHERE project_id=? AND name=?",
            (project_id, branch)).fetchone()
        return row["head_event_id"] if row else None

    def _next_seq(self, project_id: str, branch: str, parent_event_id: str | None) -> int:
        if parent_event_id is None:
            return 1
        row = self.conn.execute("SELECT seq FROM events WHERE event_id=?",
                                (parent_event_id,)).fetchone()
        if row is None:
            raise StoreError(f"unknown parent event {parent_event_id}")
        return row["seq"] + 1

    def append(self, project_id: str, branch: str, author: str, etype: str,
               payload: dict, created_at: str,
               parent_event_id: str | None = None) -> dict:
        if parent_event_id is None:
            parent_event_id = self.head(project_id, branch)
        seq = self._next_seq(project_id, branch, parent_event_id)
        full_payload = dict(payload)
        full_payload.setdefault("project_id", project_id)
        event_id = event_content_hash(parent_event_id, full_payload)
        chash = "sha256:" + hashlib.sha256(
            canonical(full_payload).encode("utf-8")).hexdigest()
        try:
            self.conn.execute(
                "INSERT INTO events(event_id,project_id,seq,branch,parent_event_id,"
                "author,created_at,type,payload_json,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event_id, project_id, seq, branch, parent_event_id, author,
                 created_at, etype, canonical(full_payload), chash))
            self.conn.execute(
                "UPDATE branches SET head_event_id=? WHERE project_id=? AND name=?",
                (event_id, project_id, branch))
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise StoreError(f"event append failed: {exc}") from exc
        return self.get_event(event_id)

    def get_event(self, event_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM events WHERE event_id=?",
                                (event_id,)).fetchone()
        if row is None:
            raise StoreError(f"unknown event {event_id}")
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def ancestry(self, project_id: str, event_id: str) -> list[dict]:
        """从根到给定事件的事件链（沿 parent_event_id）。"""
        chain = []
        current = event_id
        seen = set()
        while current and current not in seen:
            seen.add(current)
            event = self.get_event(current)
            chain.append(event)
            current = event["parent_event_id"]
        chain.reverse()
        return chain

    def tip_state_events(self, project_id: str, branch: str) -> list[dict]:
        head = self.head(project_id, branch)
        return self.ancestry(project_id, head) if head else []

    def all_events(self, project_id: str) -> list[dict]:
        return [self.get_event(r["event_id"]) for r in self.conn.execute(
            "SELECT event_id FROM events WHERE project_id=? ORDER BY created_at, seq",
            (project_id,)).fetchall()]

    # ---- export / import ----
    def export_project(self, project_id: str) -> dict:
        events = []
        for r in self.conn.execute(
                "SELECT event_id,seq,branch,parent_event_id,author,created_at,type,"
                "payload_json,content_hash FROM events WHERE project_id=? ORDER BY rowid",
                (project_id,)).fetchall():
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            events.append(d)
        project = self.conn.execute("SELECT * FROM projects WHERE project_id=?",
                                    (project_id,)).fetchone()
        bundle = {"format": "nmr-evidence-room-bundle", "format_version": 1,
                  "project": dict(project), "branches": self.branches(project_id),
                  "events": events}
        bundle["bundle_hash"] = "sha256:" + hashlib.sha256(
            canonical({"events": events, "branches": bundle["branches"],
                       "project": bundle["project"]}).encode("utf-8")).hexdigest()
        return bundle

    def import_project(self, bundle: dict, verify: bool = True) -> str:
        if bundle.get("format") != "nmr-evidence-room-bundle":
            raise StoreError("not an evidence-room bundle")
        events = bundle["events"]
        if verify:
            for event in events:
                payload = event["payload"]
                if event_content_hash(event["parent_event_id"], payload) != event["event_id"]:
                    raise StoreError(f"event content mismatch at seq {event['seq']}")
            recomputed = "sha256:" + hashlib.sha256(
                canonical({"events": events, "branches": bundle["branches"],
                           "project": bundle["project"]}).encode("utf-8")).hexdigest()
            if recomputed != bundle.get("bundle_hash"):
                raise StoreError("bundle hash mismatch; refuses import")
        project = bundle["project"]
        pid = project["project_id"]
        if self.conn.execute("SELECT 1 FROM projects WHERE project_id=?", (pid,)).fetchone():
            raise StoreError(f"project already imported: {pid}")
        self.register_project(pid, project["name"], project["created_at"])
        for event in events:
            self.conn.execute(
                "INSERT INTO events(event_id,project_id,seq,branch,parent_event_id,"
                "author,created_at,type,payload_json,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event["event_id"], pid, event["seq"], event["branch"],
                 event["parent_event_id"], event["author"], event["created_at"],
                 event["type"], canonical(event["payload"]), event["content_hash"]))
        for branch in bundle["branches"]:
            self.conn.execute(
                "INSERT INTO branches(project_id,name,head_event_id,created_at) "
                "VALUES(?,?,?,?)",
                (pid, branch["name"], branch["head_event_id"], branch["created_at"]))
        self.conn.commit()
        return pid
