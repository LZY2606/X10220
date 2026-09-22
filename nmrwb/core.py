"""核心层：确定性身份、追加式事件存储、状态机投影与过期标注。

设计原则：
- 原始事件一经写入不可覆盖，后续修订以新版本和父版本关系表达；
- 所有实体身份由内容哈希派生，导出/删除数据库/再导入后身份与顺序保持；
- 过期结果仍可查看，但不冒充当前结果；回到任意旧事件可恢复当时的
  依赖版本和可见结论。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Callable, Dict, List, Optional

ALGORITHM_VERSION = "assign-1.0.0"

DEFAULT_PARAMS = {
    "algorithm": ALGORITHM_VERSION,
    "shift_tolerance_ppm": 0.30,
    "overlap_tolerance_ppm": 0.05,
    "tie_epsilon": 1e-6,
    "edge_evidence_weight": 0.10,
    "bias_threshold_ppm": 0.03,
}


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def fingerprint(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()[:16]


def stable_id(kind: str, *parts) -> str:
    """由内容派生的稳定身份：同样内容在任何数据库中都得到同一身份。"""
    h = hashlib.sha256()
    h.update(kind.encode("utf-8"))
    for part in parts:
        h.update(b"|")
        h.update(str(part).encode("utf-8"))
    return "%s_%s" % (kind, h.hexdigest()[:12])


class Store:
    """追加式 SQLite 事件存储。clock 可注入，测试使用虚拟时间。"""

    def __init__(self, path: str = ":memory:",
                 clock: Optional[Callable[[], str]] = None):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.clock = clock or (lambda: "2026-01-01T00:00:00Z")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS events("
            "seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE, branch TEXT, "
            "kind TEXT, payload TEXT, clock TEXT)")
        self.conn.commit()

    def next_seq(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS s FROM events").fetchone()
        return int(row["s"])

    def append(self, kind, payload, branch="main", event_id=None,
               seq=None, clock=None) -> Dict:
        seq = seq if seq is not None else self.next_seq()
        event_id = event_id or stable_id("evt", seq, kind, canonical(payload))
        clock = clock or self.clock()
        self.conn.execute(
            "INSERT INTO events(seq,event_id,branch,kind,payload,clock)"
            " VALUES(?,?,?,?,?,?)",
            (seq, event_id, branch, kind, canonical(payload), clock))
        self.conn.commit()
        return {"seq": seq, "event_id": event_id, "branch": branch,
                "kind": kind, "payload": payload, "clock": clock}

    def events(self, upto: Optional[int] = None) -> List[Dict]:
        if upto is None:
            rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE seq<=? ORDER BY seq", (upto,)).fetchall()
        return [{"seq": r["seq"], "event_id": r["event_id"], "branch": r["branch"],
                 "kind": r["kind"], "payload": json.loads(r["payload"]),
                 "clock": r["clock"]} for r in rows]

    def reset(self):
        self.conn.execute("DELETE FROM events")
        self.conn.commit()

    def export_jsonl(self) -> str:
        return "\n".join(canonical(e) for e in self.events()) + "\n"

    def import_jsonl(self, text: str) -> int:
        n = 0
        for line in text.strip().splitlines():
            e = json.loads(line)
            self.append(e["kind"], e["payload"], branch=e["branch"],
                        event_id=e["event_id"], seq=e["seq"], clock=e["clock"])
            n += 1
        return n


def empty_state() -> Dict:
    return {
        "input_versions": {},  # input_id -> 输入版本（含原文与诊断）
        "inputs": {},          # name -> 最新 input_id
        "params": [],          # 参数版本列表（算法版本 + 参数指纹）
        "candidate_sets": {},  # set_id -> 候选集
        "decisions": {},       # decision_id -> 人工决定
        "conflicts": {},       # conflict_id -> 冲突
        "branches": {"main": {"parent": None, "created_seq": 0, "by": "system"}},
        "invalidations": [],   # 失效日志（机器可读事件）
        "timeline": [],
    }


def _log_invalidation(state, seq, kind, old_ref, new_ref):
    affected = [s["set_id"] for s in state["candidate_sets"].values()
                if old_ref and old_ref in s.get("depends_on", [])]
    state["invalidations"].append({
        "seq": seq, "kind": kind, "old": old_ref, "new": new_ref,
        "affected_sets": affected,
        "rule": "局部输入变化只使依赖它的结果过期；过期结果仍可查看、可重放",
    })


def apply_event(state, event):
    kind = event["kind"]
    p = event["payload"]
    seq = event["seq"]
    state["timeline"].append({"seq": seq, "kind": kind,
                              "event_id": event["event_id"],
                              "branch": event["branch"], "clock": event["clock"]})
    if kind == "input_submitted":
        parent = p.get("parent")
        state["input_versions"][p["input_id"]] = p
        state["inputs"][p["name"]] = p["input_id"]
        if parent:
            old = state["input_versions"].get(parent)
            if old is not None:
                old["superseded_by"] = p["input_id"]
            _log_invalidation(state, seq, "input", parent, p["input_id"])
    elif kind == "params_revised":
        state["params"].append(p)
        _log_invalidation(state, seq, "params", p.get("parent"), p["params_id"])
    elif kind == "candidates_computed":
        state["candidate_sets"][p["set_id"]] = p
    elif kind == "decision":
        state["decisions"][p["decision_id"]] = p
        state["branches"][p["new_branch"]] = {
            "parent": p["branch"], "created_seq": seq, "by": p["editor"]}
        # 两个编辑者从同一母分支对同一核给出不同候选 -> 冲突事件
        for d in list(state["decisions"].values()):
            if d["decision_id"] == p["decision_id"] or d.get("undone"):
                continue
            if (d["atom_id"] == p["atom_id"] and d["branch"] == p["branch"]
                    and d["candidate_id"] != p["candidate_id"]
                    and d["editor"] != p["editor"]):
                cid = stable_id("conflict", d["decision_id"], p["decision_id"])
                if cid not in state["conflicts"]:
                    state["conflicts"][cid] = {
                        "conflict_id": cid, "atom_id": p["atom_id"],
                        "decisions": [d["decision_id"], p["decision_id"]],
                        "editors": [d["editor"], p["editor"]],
                        "status": "open", "detected_seq": seq}
    elif kind == "conflict_resolved":
        c = state["conflicts"].get(p["conflict_id"])
        if c:
            c["status"] = "resolved"
            c["resolution"] = p
    elif kind == "undo":
        d = state["decisions"].get(p["target_decision_id"])
        if d:
            d["undone"] = True
            d["undone_by"] = p["editor"]
    return state


def fold(events) -> Dict:
    state = empty_state()
    for e in events:
        apply_event(state, e)
    return state


def annotate(state) -> Dict:
    """按当前折叠状态标注过期信息。

    过期是相对于“该状态所知的最新依赖版本”计算的，因此重放到旧事件时，
    旧候选集会恢复为当时的“当前”状态，而不是被最新计算结果覆盖。
    """
    latest_inputs = set(state["inputs"].values())
    latest_params = state["params"][-1]["params_id"] if state["params"] else None

    def is_current(dep):
        return dep in latest_inputs or dep == latest_params

    sets = {}
    for sid, s in state["candidate_sets"].items():
        cands = []
        for c in s["candidates"]:
            c = dict(c)
            c["stale"] = not all(is_current(d) for d in c["depends_on"])
            cands.append(c)
        entry = dict(s)
        entry["candidates"] = cands
        entry["stale"] = not all(is_current(d) for d in s["depends_on"])
        sets[sid] = entry
    out = dict(state)
    out["candidate_sets"] = sets
    return out
