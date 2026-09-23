"""事件溯源核心：SQLite 事件存储、分支、虚拟时钟、状态投影。

所有事实都是不可变事件；当前状态只是事件日志在某个分支、某个序号上的投影。
回到任意旧事件即可恢复当时的依赖版本与可见结论。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY,
  uuid TEXT NOT NULL,
  branch TEXT NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  actor TEXT NOT NULL,
  vtime TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches(
  name TEXT PRIMARY KEY,
  fork_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

VIRTUAL_EPOCH = "2026-01-01T00:00:00+00:00"


class VirtualClock:
    """确定性虚拟时钟：从固定纪元起每事件走一秒，测试与演示完全可复现。"""

    def __init__(self, start: str = VIRTUAL_EPOCH, step_seconds: int = 1):
        self.start = datetime.fromisoformat(start)
        if self.start.tzinfo is None:
            self.start = self.start.replace(tzinfo=timezone.utc)
        self.step = timedelta(seconds=step_seconds)
        self.n = 0

    def tick(self) -> str:
        self.n += 1
        return (self.start + self.n * self.step).isoformat()

    def fast_forward(self, n: int) -> None:
        self.n = max(self.n, n)


class EventStore:
    def __init__(self, path: str, clock: VirtualClock | None = None, id_fn=None):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.clock = clock or VirtualClock()
        self.id_fn = id_fn or (lambda: uuid.uuid4().hex)
        self.conn.execute("INSERT OR IGNORE INTO branches(name, fork_seq) VALUES('main', 0)")
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- 写入 ----
    def append(self, type_: str, payload: dict, actor: str = "system", branch: str = "main") -> dict:
        seq = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS s FROM events").fetchone()["s"]
        event = {
            "seq": seq,
            "uuid": self.id_fn(),
            "branch": branch,
            "type": type_,
            "payload": payload,
            "actor": actor,
            "vtime": self.clock.tick(),
        }
        self.conn.execute(
            "INSERT INTO events(seq,uuid,branch,type,payload,actor,vtime) VALUES(?,?,?,?,?,?,?)",
            (seq, event["uuid"], branch, type_, json.dumps(payload, sort_keys=True), actor, event["vtime"]),
        )
        self.conn.commit()
        return event

    def create_branch(self, name: str, fork_seq: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO branches(name, fork_seq) VALUES(?,?)", (name, fork_seq))
        self.conn.commit()

    # ---- 读取 ----
    def branches(self) -> list[dict]:
        rows = self.conn.execute("SELECT name, fork_seq FROM branches ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def fork_seq(self, branch: str) -> int:
        row = self.conn.execute("SELECT fork_seq FROM branches WHERE name=?", (branch,)).fetchone()
        return row["fork_seq"] if row else 0

    def max_seq(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM events").fetchone()["s"]

    def all_events(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [self._row_to_event(r) for r in rows]

    def visible_events(self, branch: str = "main", upto_seq: int | None = None) -> list[dict]:
        """分支可见性：main 上 fork 点之前的事件 + 该分支自己的事件。"""
        limit = upto_seq if upto_seq is not None else self.max_seq()
        if branch == "main":
            rows = self.conn.execute(
                "SELECT * FROM events WHERE branch='main' AND seq<=? ORDER BY seq", (limit,)
            ).fetchall()
        else:
            fork = self.fork_seq(branch)
            rows = self.conn.execute(
                "SELECT * FROM events WHERE ((branch='main' AND seq<=?) OR branch=?) AND seq<=? ORDER BY seq",
                (fork, branch, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> dict:
        return {
            "seq": r["seq"],
            "uuid": r["uuid"],
            "branch": r["branch"],
            "type": r["type"],
            "payload": json.loads(r["payload"]),
            "actor": r["actor"],
            "vtime": r["vtime"],
        }

    # ---- 导出 / 导入（身份、顺序、来源关系原样保留）----
    def export(self) -> dict:
        return {
            "schema": "nmrwb-export/1",
            "branches": self.branches(),
            "events": self.all_events(),
        }

    def import_export(self, data: dict) -> None:
        if data.get("schema") != "nmrwb-export/1":
            raise ValueError("unsupported export schema")
        if self.max_seq() != 0:
            raise ValueError("import requires an empty database")
        for b in data.get("branches", []):
            self.conn.execute("INSERT OR IGNORE INTO branches(name, fork_seq) VALUES(?,?)", (b["name"], b["fork_seq"]))
        for e in data.get("events", []):
            self.conn.execute(
                "INSERT INTO events(seq,uuid,branch,type,payload,actor,vtime) VALUES(?,?,?,?,?,?,?)",
                (e["seq"], e["uuid"], e["branch"], e["type"], json.dumps(e["payload"], sort_keys=True), e["actor"], e["vtime"]),
            )
        self.conn.commit()
        self.clock.fast_forward(len(data.get("events", [])))


# ---------------------------------------------------------------------------
# 投影：把事件日志折叠成可查询的当前（或历史）状态
# ---------------------------------------------------------------------------

def empty_state(branch: str) -> dict:
    return {
        "branch": branch,
        "seq": 0,
        "sources": {},          # stream -> {"current": rec, "versions": [rec...]}
        "compound": None,
        "params": None,
        "params_history": [],
        "candidate_sets": [],
        "expired_candidates": {},  # candidate_uuid -> {reason, event_seq, trigger_seq}
        "decisions": [],
        "conflicts": [],
        "judgments": [],
        "diagnostics": [],
        "events": [],
    }


def project(store: EventStore, branch: str = "main", upto_seq: int | None = None) -> dict:
    events = store.visible_events(branch, upto_seq)
    st = empty_state(branch)
    st["seq"] = upto_seq if upto_seq is not None else (events[-1]["seq"] if events else 0)
    st["events"] = events
    for e in events:
        _apply(st, e)
    _finalize(st)
    return st


def _apply(st: dict, e: dict) -> None:
    t = e["type"]
    p = e["payload"]
    if t == "source_imported":
        stream = p["stream"]
        slot = st["sources"].setdefault(stream, {"current": None, "versions": []})
        rec = dict(p)
        rec["event_seq"] = e["seq"]
        rec["event_uuid"] = e["uuid"]
        slot["versions"].append(rec)
        slot["current"] = rec
        for d in p.get("diagnostics", []):
            st["diagnostics"].append(dict(d, stream=stream, version=p["version"], event_seq=e["seq"]))
        if p["kind"] == "compound":
            st["compound"] = p["parsed"]
    elif t == "params_set":
        rec = dict(p)
        rec["event_seq"] = e["seq"]
        st["params"] = rec
        st["params_history"].append(rec)
    elif t == "candidates_computed":
        rec = dict(p)
        rec["event_seq"] = e["seq"]
        rec["event_uuid"] = e["uuid"]
        st["candidate_sets"].append(rec)
    elif t == "candidates_expired":
        for cid in p["candidate_uuids"]:
            st["expired_candidates"][cid] = {
                "reason": p["reason"],
                "event_seq": e["seq"],
                "trigger_seq": p.get("trigger_seq"),
            }
    elif t == "decision_made":
        rec = dict(p)
        rec.update({"uuid": e["uuid"], "seq": e["seq"], "branch": e["branch"], "actor": e["actor"], "vtime": e["vtime"]})
        st["decisions"].append(rec)
    elif t == "decision_reverted":
        rec = dict(p)
        rec.update({"uuid": e["uuid"], "seq": e["seq"]})
        st.setdefault("reverts", []).append(rec)
    elif t == "conflict_recorded":
        rec = dict(p)
        rec.update({"uuid": e["uuid"], "seq": e["seq"]})
        st["conflicts"].append(rec)
    elif t == "conflict_resolved":
        rec = dict(p)
        rec = dict(p)
        rec.update({"uuid": e["uuid"], "seq": e["seq"]})
        st.setdefault("resolutions", []).append(rec)
    elif t == "judgment_recorded":
        rec = dict(p)
        rec.update({"uuid": e["uuid"], "seq": e["seq"]})
        st["judgments"].append(rec)


def _finalize(st: dict) -> None:
    # 候选集新旧关系
    for i, cs in enumerate(st["candidate_sets"]):
        cs["status"] = "current" if i == len(st["candidate_sets"]) - 1 else "superseded"
        for cand in cs.get("candidates", []):
            exp = st["expired_candidates"].get(cand["candidate_uuid"])
            cand["expired"] = bool(exp)
            cand["expired_reason"] = exp["reason"] if exp else None
    current = st["candidate_sets"][-1] if st["candidate_sets"] else None
    current_index = {}
    if current:
        for cand in current["candidates"]:
            if not cand["expired"]:
                current_index[(cand["nucleus"], cand["peak_id"])] = cand["candidate_uuid"]

    reverted = {r["revert_of"] for r in st.get("reverts", [])}
    resolutions = st.get("resolutions", [])
    overruled, resolved_conflicts = set(), set()
    for r in resolutions:
        resolved_conflicts.add(r["conflict_uuid"])
        for d_uuid in r.get("losing_decision_uuids", []):
            overruled.add(d_uuid)

    # 同一编辑者对同一核的后续决定覆盖自己先前的决定
    latest_by_editor = {}
    for d in st["decisions"]:
        latest_by_editor[(d["nucleus"], d["editor"])] = d["uuid"]

    for d in st["decisions"]:
        if d["uuid"] in reverted:
            d["status"] = "reverted"
        elif d["uuid"] in overruled:
            d["status"] = "overruled"
        elif latest_by_editor[(d["nucleus"], d["editor"])] != d["uuid"]:
            d["status"] = "superseded"
        else:
            d["status"] = "active"
        d["current_candidate_uuid"] = current_index.get((d["nucleus"], d["peak_id"]))
        d["currency"] = "current" if d["current_candidate_uuid"] else "stale"

    for c in st["conflicts"]:
        c["resolved"] = c["uuid"] in resolved_conflicts
        for r in resolutions:
            if r["conflict_uuid"] == c["uuid"]:
                c["resolution"] = r

    st["current_set"] = current
    st["decisions_active"] = [d for d in st["decisions"] if d["status"] == "active"]
