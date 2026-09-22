"""事件溯源 + SQLite 持久化与重放。

事件链：prev_hash 构成可验证顺序；派生状态（dataset/snapshot）一律由事件
重放产生，保证“回到任意旧事件时恢复当时依赖版本与可见结论”。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .ident import canonical, chain_digest, digest, fingerprint
from .clock import Clock
from .parser import parse_peak_list, ParsedDocument
from .model import build_dataset, Dataset
from .params import normalize_params, params_fingerprint
from . import engine as eng

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER NOT NULL,
    branch TEXT NOT NULL,
    id TEXT PRIMARY KEY,
    prev_hash TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(branch, seq)
);
CREATE TABLE IF NOT EXISTS branches (
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    head_event_id TEXT,
    parent_event_id TEXT,
    reason TEXT,
    created_at TEXT,
    PRIMARY KEY(project_id, name)
);
CREATE TABLE IF NOT EXISTS kv (
    project_id TEXT, branch TEXT, key TEXT, value TEXT,
    PRIMARY KEY(project_id, branch, key)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def context_fp(context: Dict[str, Any]) -> str:
    material = {"solvent": context.get("solvent"),
                "temperature_k": (context.get("temperature_k") or {}).get("value")
                if isinstance(context.get("temperature_k"), dict)
                else context.get("temperature_k"),
                "reference_standard": context.get("reference_standard")}
    return "ctx_" + digest(material)[:14]


class EventError(Exception):
    pass


class Project:
    def __init__(self, db_path: str, project_id: str, clock: Optional[Clock] = None):
        self.db_path = db_path
        self.pid = project_id
        self.clock = clock or Clock()
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- 基础事件写入 ----------
    def _last_hash_on_ancestry(self, branch: str) -> Optional[str]:
        rows = self._ancestry_events(branch)
        return rows[-1]["id"] if rows else None

    def append(self, etype: str, payload: Dict[str, Any], branch: str = "main",
               ts: Optional[str] = None) -> Dict[str, Any]:
        prev = self._last_hash_on_ancestry(branch)
        prev_hash = prev or "GENESIS"
        eid = chain_digest(prev_hash, etype, payload)
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE branch=?",
            (branch,)).fetchone()[0]
        created = ts or self.clock.now()
        self.conn.execute(
            "INSERT INTO events(seq,branch,id,prev_hash,type,payload,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (seq, branch, eid, prev_hash, etype, canonical(payload), created))
        self.conn.execute(
            "INSERT OR REPLACE INTO branches(project_id,name,head_event_id,"
            "parent_event_id,reason,created_at) VALUES(?,?,?,?,?,?)",
            (self.pid, branch, eid, prev,
             payload.get("reason") if etype == "BranchCreated" else None,
             created))
        self.conn.commit()
        return {"seq": seq, "branch": branch, "id": eid, "prev_hash": prev_hash,
                "type": etype, "payload": payload, "created_at": created}

    def create_branch(self, name: str, from_event_id: str, reason: str,
                      editor: Optional[str] = None) -> Dict[str, Any]:
        if self.conn.execute("SELECT 1 FROM branches WHERE project_id=? AND name=?",
                             (self.pid, name)).fetchone():
            raise EventError(f"分支已存在: {name}")
        ev = self._event_by_id(from_event_id)
        if ev is None:
            raise EventError("分叉点事件不存在")
        payload = {"name": name, "from_event_id": from_event_id,
                   "reason": reason, "editor": editor}
        return self.append("BranchCreated", payload, branch=name)

    def _event_by_id(self, eid: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events WHERE id=?",
                                 (eid,)).fetchone()

    # ---------- 祖先链 ----------
    def _branch_parent(self, branch: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT parent_event_id FROM branches WHERE project_id=? AND name=?",
            (self.pid, branch)).fetchone()
        return row["parent_event_id"] if row else None

    def _ancestry_events(self, branch: str,
                         upto_event_id: Optional[str] = None) -> List[sqlite3.Row]:
        """返回该分支可见的事件：自身分支全部事件（祖先点之后）+ 父链。"""
        own = list(self.conn.execute(
            "SELECT * FROM events WHERE branch=? ORDER BY seq", (branch,)))
        parent_id = self._branch_parent(branch)
        if upto_event_id:
            own = own[: self._prefix_len(own, upto_event_id)]
        result: List[sqlite3.Row] = []
        if parent_id:
            parent_row = self._event_by_id(parent_id)
            parent_branch = parent_row["branch"]
            result.extend(self._ancestry_events(parent_branch,
                                                upto_event_id=parent_id))
        result.extend(own)
        return result

    @staticmethod
    def _prefix_len(rows: List[sqlite3.Row], eid: str) -> int:
        for i, r in enumerate(rows, start=1):
            if r["id"] == eid:
                return i
        return len(rows)

    def list_branches(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM branches WHERE project_id=? ORDER BY name",
            (self.pid,)).fetchall()
        return [dict(r) for r in rows]

    def events(self, branch: str = "main",
               upto_event_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [self._row_to_event(r)
                for r in self._ancestry_events(branch, upto_event_id)]

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> Dict[str, Any]:
        return {"seq": r["seq"], "branch": r["branch"], "id": r["id"],
                "prev_hash": r["prev_hash"], "type": r["type"],
                "payload": json.loads(r["payload"]),
                "created_at": r["created_at"]}


    # ---------- 高层用例操作 ----------
    def submit_source(self, slot: str, version: str, raw_text: str,
                      parent_id: Optional[str] = None,
                      editor: str = "importer") -> Dict[str, Any]:
        doc = parse_peak_list(raw_text)
        if doc.has_errors:
            raise EventError("原始文档含 error 级格式问题，拒绝提交: "
                             + "; ".join(f"行{d.lineno}:{d.message}"
                                         for d in doc.diagnostics
                                         if d.level == "error"))
        source_id = doc.source_hash
        payload = {"slot": slot, "version": version, "source_id": source_id,
                   "parent_id": parent_id, "raw_text": raw_text,
                   "format": doc.format, "editor": editor}
        return self.append("SourceSubmitted", payload)

    def set_params(self, params: Dict[str, Any], editor: str = "default",
                   note: str = "") -> Dict[str, Any]:
        norm = normalize_params(params)
        pfp = params_fingerprint(norm)
        return self.append("ParametersSet",
                           {"params": norm, "fingerprint": pfp,
                            "editor": editor, "note": note})

    def change_context(self, changes: Dict[str, Any], reason: str,
                       editor: str = "default") -> Dict[str, Any]:
        return self.append("ContextChanged",
                           {"changes": changes, "reason": reason,
                            "editor": editor})

    def compute_snapshot(self, clock_ts: Optional[str] = None) -> Dict[str, Any]:
        from .replay import replay
        from .engine import run_engine
        state = replay(self, self._current_branch_name())
        if not state.dataset:
            raise EventError("尚无源数据，无法计算候选")
        snap = run_engine(state.dataset, state.params,
                          clock_ts or self.clock.now(),
                          state.context_fingerprint, state.inputs_hash,
                          [s["id"] for s in state.sources if s["is_current"]])
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(json_extract(payload,'$.seq')),0)+1 "
            "FROM events WHERE type='SnapshotComputed'").fetchone()[0]
        payload = {"seq": seq, "snapshot": snap.to_dict()}
        return self.append("SnapshotComputed", payload)

    def decide(self, nuclei: List[str], action: str, editor: str,
               based_on_snapshot: str, candidate_id: Optional[str] = None,
               candidate_ids: Optional[List[str]] = None,
               ambiguity_group: Optional[str] = None, note: str = "") -> Dict[str, Any]:
        if action not in ("accept", "reject"):
            raise EventError("action 必须为 accept/reject")
        payload = {"nuclei": sorted(nuclei), "action": action, "editor": editor,
                   "candidate_id": candidate_id,
                   "candidate_ids": sorted(candidate_ids) if candidate_ids else None,
                   "ambiguity_group": ambiguity_group,
                   "based_on_snapshot": based_on_snapshot, "note": note}
        return self.append("DecisionMade", payload)

    def revert_decision(self, revert_event_id: str, editor: str,
                        note: str = "") -> Dict[str, Any]:
        return self.append("DecisionReverted",
                           {"revert_event_id": revert_event_id,
                            "editor": editor, "note": note})

    def resolve_conflict(self, nuclei: List[str], winning_editor: str,
                         winning_event_id: str, editor: str,
                         note: str = "") -> Dict[str, Any]:
        return self.append("ConflictResolved",
                           {"nuclei": sorted(nuclei),
                            "winning_editor": winning_editor,
                            "winning_event_id": winning_event_id,
                            "editor": editor, "note": note})

    def _current_branch_name(self) -> str:
        return getattr(self, "active_branch", "main")
