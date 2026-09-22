from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from .core import Clock, canonical, stable_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY,
  id TEXT UNIQUE NOT NULL,
  branch TEXT NOT NULL,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

ARCHIVE_FORMAT = "nmrwb-archive/1"


class State:
    """Deterministic projection rebuilt by replaying events."""

    def __init__(self):
        self.docs = {}          # doc_id -> document (incl. raw text, parsed data, diagnostics)
        self.current = {}       # stream -> doc_id
        self.doc_versions = {}  # stream -> [doc_id, ...] in version order
        self.candidates = {}    # cand_id -> candidate record
        self.runs = {}          # run_id -> run record
        self.decisions = {}     # decision_id -> decision
        self.conflicts = {}     # conflict_id -> conflict
        self.branches = {"main": {"parent": None, "fork_seq": 0}}
        self.judgments = []     # machine-readable case judgments
        self.diagnostics = []   # flattened diagnostics with stable ids
        self.current_branch = "main"
        self.seq = 0

    # -- branch visibility -------------------------------------------------
    def ancestry(self, branch: str):
        """[(branch, visible_up_to_seq)] from leaf to root."""
        chain = []
        limit = 1 << 62
        b = branch
        while b is not None:
            chain.append((b, limit))
            info = self.branches.get(b)
            if info is None:
                break
            limit = info["fork_seq"]
            b = info["parent"]
        return chain


def _apply(state: State, ev: dict) -> None:
    p = ev["payload"]
    t = ev["type"]
    state.seq = max(state.seq, ev["seq"])
    if t == "document_submitted":
        doc = dict(p)
        state.docs[doc["doc_id"]] = doc
        state.current[doc["stream"]] = doc["doc_id"]
        state.doc_versions.setdefault(doc["stream"], []).append(doc["doc_id"])
        for d in doc.get("diagnostics", []):
            dd = dict(d)
            dd["doc_id"] = doc["doc_id"]
            state.diagnostics.append(dd)
    elif t == "invalidation_applied":
        for cid in p["affected"]:
            c = state.candidates.get(cid)
            if c is not None and c["status"] == "current":
                c["status"] = "stale"
                c["stale_reason"] = p["reason"]
    elif t == "derived_computed":
        run = p
        state.runs[run["run_id"]] = run
        new_ids = {c["cand_id"] for c in run["candidates"]}
        for cid, c in state.candidates.items():
            if c["status"] == "current" and cid not in new_ids:
                c["status"] = "superseded"
        for c in run["candidates"]:
            rec = dict(c)
            rec["status"] = "current"
            rec["run_id"] = run["run_id"]
            rec.pop("stale_reason", None)
            state.candidates[rec["cand_id"]] = rec
    elif t == "decision_made":
        dec = dict(p)
        dec["seq"] = ev["seq"]
        dec["branch"] = ev["branch"]
        dec["at"] = ev["at"]
        dec["status"] = "active"
        state.decisions[dec["decision_id"]] = dec
        nb = p.get("new_branch")
        if nb:
            state.branches[nb] = {"parent": ev["branch"], "fork_seq": ev["seq"]}
            state.current_branch = nb
    elif t == "decision_reverted":
        tgt = state.decisions.get(p["target"])
        if tgt is not None:
            tgt["status"] = "reverted"
    elif t == "conflict_detected":
        state.conflicts[p["conflict_id"]] = dict(p, status="open")
    elif t == "conflict_resolved":
        c = state.conflicts.get(p["conflict_id"])
        if c is not None:
            c["status"] = "resolved"
            c["resolution"] = p
    elif t == "case_judgment":
        state.judgments.append(dict(p, seq=ev["seq"], at=ev["at"]))


def _post_process(state: State) -> None:
    """Attach active decisions to candidates (undo-aware)."""
    for c in state.candidates.values():
        c["decision"] = None
    for dec in sorted(state.decisions.values(), key=lambda d: d["seq"]):
        if dec["status"] != "active":
            continue
        cand = state.candidates.get(dec["candidate_id"])
        if cand is not None:
            cand["decision"] = {
                "decision_id": dec["decision_id"],
                "verdict": dec["verdict"],
                "actor": dec["actor"],
                "unique": dec.get("unique", False),
            }


class Store:
    def __init__(self, path: str, clock: Optional[Clock] = None):
        self.clock = clock or Clock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- appending ----------------------------------------------------------
    def peek_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0) AS m FROM events").fetchone()
        return int(row["m"]) + 1

    def current_branch(self) -> str:
        row = self.conn.execute("SELECT v FROM kv WHERE k='current_branch'").fetchone()
        return row["v"] if row else "main"

    def _set_current_branch(self, branch: str):
        self.conn.execute(
            "INSERT INTO kv(k,v) VALUES('current_branch',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (branch,),
        )

    def append(self, type_: str, payload: dict, actor: str = "system",
               branch: Optional[str] = None, switch_branch: Optional[str] = None) -> dict:
        branch = branch or self.current_branch()
        seq = self.peek_seq()
        eid = stable_id("evt", branch, seq, type_, payload)
        at = self.clock.iso()
        self.conn.execute(
            "INSERT INTO events(seq,id,branch,type,actor,at,payload) VALUES(?,?,?,?,?,?,?)",
            (seq, eid, branch, type_, actor, at, canonical(payload)),
        )
        if switch_branch:
            self._set_current_branch(switch_branch)
        self.conn.commit()
        return {"seq": seq, "id": eid, "branch": branch, "type": type_,
                "actor": actor, "at": at, "payload": payload}

    # -- reading ------------------------------------------------------------
    def events(self) -> List[dict]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [{"seq": r["seq"], "id": r["id"], "branch": r["branch"],
                 "type": r["type"], "actor": r["actor"], "at": r["at"],
                 "payload": json.loads(r["payload"])} for r in rows]

    def replay(self, as_of_seq: Optional[int] = None, branch: Optional[str] = None) -> State:
        state = State()
        all_events = self.events()
        # build branch table first so ancestry is available for filtering
        for ev in all_events:
            if ev["type"] == "decision_made" and ev["payload"].get("new_branch"):
                state.branches[ev["payload"]["new_branch"]] = {
                    "parent": ev["branch"], "fork_seq": ev["seq"]}
        branch = branch or self.current_branch()
        if as_of_seq is None:
            as_of_seq = all_events[-1]["seq"] if all_events else 0
        chain = dict(state.ancestry(branch))
        for ev in all_events:
            limit = chain.get(ev["branch"])
            if limit is None:
                continue
            if ev["seq"] <= min(limit, as_of_seq):
                _apply(state, ev)
        state.current_branch = branch
        _post_process(state)
        return state

    # -- export / import ------------------------------------------------------
    def export_archive(self) -> dict:
        return {"format": ARCHIVE_FORMAT, "events": self.events()}

    def import_archive(self, archive: dict) -> dict:
        if archive.get("format") != ARCHIVE_FORMAT:
            raise ValueError("unknown archive format: %r" % archive.get("format"))
        imported, skipped = 0, 0
        for ev in archive.get("events", []):
            exists = self.conn.execute("SELECT 1 FROM events WHERE id=?", (ev["id"],)).fetchone()
            if exists:
                skipped += 1
                continue
            clash = self.conn.execute("SELECT id FROM events WHERE seq=?", (ev["seq"],)).fetchone()
            if clash:
                raise ValueError("seq %d already used by %s" % (ev["seq"], clash["id"]))
            self.conn.execute(
                "INSERT INTO events(seq,id,branch,type,actor,at,payload) VALUES(?,?,?,?,?,?,?)",
                (ev["seq"], ev["id"], ev["branch"], ev["type"], ev["actor"], ev["at"],
                 canonical(ev["payload"])))
            imported += 1
        self.conn.commit()
        return {"imported": imported, "skipped": skipped}

    def reset(self):
        self.conn.execute("DELETE FROM events")
        self.conn.execute("DELETE FROM kv")
        self.conn.commit()
