"""SQLite 事件存储：追加式事件日志 + 分支 + 重放 + 导出/导入。

- 原始输入一经提交不可覆盖，修订以新版本 + 父版本关系表达。
- 每个事件携带稳定身份 event_id、全局顺序 seq、父事件列表（DAG）。
- 回到任意旧事件 = 只折叠该事件的祖先，恢复当时的依赖版本与可见结论。
- 导出/删除数据库/再导入后，身份、顺序与来源关系保持不变。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY,
  event_id TEXT UNIQUE NOT NULL,
  branch TEXT NOT NULL,
  parents TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  virtual_time TEXT NOT NULL,
  algo_version TEXT,
  param_fingerprint TEXT,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
  branch_id TEXT PRIMARY KEY,
  parent_branch TEXT,
  fork_event TEXT,
  created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

EXPORT_FORMAT = "nmrwb-export/1"


def default_clock() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, db_path: str, clock: Optional[Callable[[], str]] = None):
        self.db_path = db_path
        self.clock = clock or default_clock
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- 底层 ----------
    def _next_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM events").fetchone()
        return int(row["s"])

    def _event_id(self, seq: int, kind: str, payload: dict) -> str:
        digest = hashlib.sha256(
            json.dumps({"seq": seq, "kind": kind, "payload": payload}, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:10]
        return f"evt-{seq:04d}-{digest}"

    def is_empty(self) -> bool:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return row["n"] == 0

    # ---------- 分支 ----------
    def create_branch(self, parent_branch: Optional[str], fork_event: Optional[str]) -> str:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM branches").fetchone()
        branch_id = f"br-{row['n'] + 1:03d}"
        seq = self._next_seq()
        self.conn.execute(
            "INSERT INTO branches (branch_id, parent_branch, fork_event, created_seq) VALUES (?,?,?,?)",
            (branch_id, parent_branch, fork_event, seq),
        )
        self.conn.commit()
        return branch_id

    def branches(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM branches ORDER BY created_seq").fetchall()
        return [dict(r) for r in rows]

    # ---------- 事件 ----------
    def append(
        self,
        kind: str,
        payload: dict,
        actor: str,
        branch: str,
        parents: Optional[list[str]] = None,
        algo_version: Optional[str] = None,
        param_fingerprint: Optional[str] = None,
    ) -> dict:
        seq = self._next_seq()
        event_id = self._event_id(seq, kind, payload)
        event = {
            "seq": seq,
            "event_id": event_id,
            "branch": branch,
            "parents": parents or [],
            "kind": kind,
            "actor": actor,
            "virtual_time": self.clock(),
            "algo_version": algo_version,
            "param_fingerprint": param_fingerprint,
            "payload": payload,
        }
        self.conn.execute(
            "INSERT INTO events (seq, event_id, branch, parents, kind, actor, virtual_time, algo_version, param_fingerprint, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                seq,
                event_id,
                branch,
                json.dumps(event["parents"], ensure_ascii=False),
                kind,
                actor,
                event["virtual_time"],
                algo_version,
                param_fingerprint,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.conn.commit()
        return event

    def events(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["parents"] = json.loads(d["parents"])
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def get_event(self, event_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["parents"] = json.loads(d["parents"])
        d["payload"] = json.loads(d["payload"])
        return d

    def ancestors(self, event_id: str) -> list[dict]:
        """按 seq 升序返回该事件的全部祖先（含自身）——重放任意历史点。"""
        all_events = {e["event_id"]: e for e in self.events()}
        if event_id not in all_events:
            raise KeyError(f"未知事件 {event_id}")
        seen: set[str] = set()
        stack = [event_id]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in all_events:
                continue
            seen.add(cur)
            stack.extend(all_events[cur]["parents"])
        return sorted((all_events[i] for i in seen), key=lambda e: e["seq"])

    # ---------- 头指针 ----------
    def set_head(self, branch: str, event_id: str) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"head:{branch}", event_id),
        )
        self.conn.commit()

    def get_head(self, branch: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (f"head:{branch}",)).fetchone()
        return row["value"] if row else None

    def current_branch(self) -> str:
        row = self.conn.execute("SELECT value FROM kv WHERE key = 'current_branch'").fetchone()
        return row["value"] if row else "main"

    def set_current_branch(self, branch: str) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES ('current_branch', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (branch,),
        )
        self.conn.commit()

    # ---------- 导出 / 导入 ----------
    def export(self) -> dict:
        return {
            "format": EXPORT_FORMAT,
            "events": self.events(),
            "branches": self.branches(),
            "kv": {r["key"]: r["value"] for r in self.conn.execute("SELECT * FROM kv").fetchall()},
        }

    def import_(self, data: dict) -> None:
        if data.get("format") != EXPORT_FORMAT:
            raise ValueError(f"不支持的导出格式 {data.get('format')!r}")
        if not self.is_empty():
            raise ValueError("目标数据库非空，拒绝导入（导出档案只允许导入空库）")
        for b in data.get("branches", []):
            self.conn.execute(
                "INSERT INTO branches (branch_id, parent_branch, fork_event, created_seq) VALUES (?,?,?,?)",
                (b["branch_id"], b["parent_branch"], b["fork_event"], b["created_seq"]),
            )
        for e in data.get("events", []):
            self.conn.execute(
                "INSERT INTO events (seq, event_id, branch, parents, kind, actor, virtual_time, algo_version, param_fingerprint, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    e["seq"],
                    e["event_id"],
                    e["branch"],
                    json.dumps(e["parents"], ensure_ascii=False),
                    e["kind"],
                    e["actor"],
                    e["virtual_time"],
                    e.get("algo_version"),
                    e.get("param_fingerprint"),
                    json.dumps(e["payload"], ensure_ascii=False, sort_keys=True),
                ),
            )
        for k, v in data.get("kv", {}).items():
            self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", (k, v))
        self.conn.commit()
