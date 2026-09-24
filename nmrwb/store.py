"""Event-sourced SQLite store for the NMR assignment workbench.

Every mutation is an immutable event. Materialized tables are a pure
function of the event log, so any historical point can be replayed by
re-applying events up to a sequence number. Export/import simply dumps
and replays the log, which preserves identity, order and provenance.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL,
  branch TEXT,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  vtime TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents(
  doc_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  parent_version INTEGER,
  format TEXT NOT NULL,
  raw TEXT NOT NULL,
  meta TEXT NOT NULL,
  event_seq INTEGER NOT NULL,
  PRIMARY KEY(doc_id, version)
);
CREATE TABLE IF NOT EXISTS diagnostics(
  diag_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  message TEXT NOT NULL,
  raw_value TEXT,
  event_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS params(
  version INTEGER PRIMARY KEY,
  parent_version INTEGER,
  values_json TEXT NOT NULL,
  affects TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  event_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY,
  branch TEXT NOT NULL,
  nucleus TEXT NOT NULL,
  algo TEXT NOT NULL,
  params_version INTEGER NOT NULL,
  params_fp TEXT NOT NULL,
  src_refs TEXT NOT NULL,
  unmatched TEXT NOT NULL,
  event_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates(
  cand_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  branch TEXT NOT NULL,
  nucleus TEXT NOT NULL,
  peak_id TEXT NOT NULL,
  atom_id TEXT NOT NULL,
  score REAL,
  rank INTEGER,
  tie_group TEXT,
  evidence TEXT NOT NULL,
  notes TEXT NOT NULL,
  status TEXT NOT NULL,
  src_docs TEXT NOT NULL,
  params_fp TEXT NOT NULL,
  event_seq INTEGER NOT NULL,
  expired_seq INTEGER,
  expire_reason TEXT
);
CREATE TABLE IF NOT EXISTS branches(
  branch_id TEXT PRIMARY KEY,
  parent TEXT,
  label TEXT NOT NULL,
  event_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions(
  decision_id TEXT PRIMARY KEY,
  branch TEXT NOT NULL,
  peak_id TEXT NOT NULL,
  atom_id TEXT NOT NULL,
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  event_seq INTEGER NOT NULL,
  undone_seq INTEGER,
  superseded_seq INTEGER
);
CREATE TABLE IF NOT EXISTS conflicts(
  conflict_id TEXT PRIMARY KEY,
  branch TEXT NOT NULL,
  peak_id TEXT NOT NULL,
  decision_ids TEXT NOT NULL,
  status TEXT NOT NULL,
  event_seq INTEGER NOT NULL,
  resolved_seq INTEGER,
  resolution TEXT
);
"""


class Store:
    """Append-only event log plus materialized views rebuilt by reducers."""

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- event log -----------------------------------------------------
    def append(self, kind: str, payload: dict, actor: str, vtime: str,
               branch: str | None = None, seq: int | None = None,
               event_id: str | None = None) -> dict:
        if seq is None:
            cur = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events")
            seq = cur.fetchone()[0]
        if event_id is None:
            event_id = f"ev-{seq:04d}"
        self.conn.execute(
            "INSERT INTO events(seq,event_id,branch,kind,actor,vtime,payload)"
            " VALUES(?,?,?,?,?,?,?)",
            (seq, event_id, branch, kind, actor, vtime, json.dumps(payload, sort_keys=True)))
        self.conn.commit()
        event = {"seq": seq, "event_id": event_id, "branch": branch,
                 "kind": kind, "actor": actor, "vtime": vtime, "payload": payload}
        self._reduce(event)
        self.conn.commit()
        return event

    def events(self, upto_seq: int | None = None) -> list[dict]:
        sql = "SELECT * FROM events"
        args: tuple = ()
        if upto_seq is not None:
            sql += " WHERE seq<=?"
            args = (upto_seq,)
        sql += " ORDER BY seq"
        out = []
        for row in self.conn.execute(sql, args):
            out.append({"seq": row["seq"], "event_id": row["event_id"],
                        "branch": row["branch"], "kind": row["kind"],
                        "actor": row["actor"], "vtime": row["vtime"],
                        "payload": json.loads(row["payload"])})
        return out

    # -- reducers ------------------------------------------------------
    def _reduce(self, ev: dict) -> None:
        k, p, s = ev["kind"], ev["payload"], ev["seq"]
        c = self.conn
        if k == "document_imported":
            c.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?)",
                      (p["doc_id"], p["version"], p.get("parent_version"),
                       p["format"], p["raw"], json.dumps(p.get("meta", {}), sort_keys=True), s))
            for i, d in enumerate(p.get("diagnostics", [])):
                c.execute("INSERT INTO diagnostics VALUES(?,?,?,?,?,?)",
                          (f"dg-{s:04d}-{i}", p["doc_id"], p["version"],
                           d["message"], d.get("raw_value"), s))
        elif k == "params_committed":
            c.execute("INSERT INTO params VALUES(?,?,?,?,?,?)",
                      (p["version"], p.get("parent_version"), json.dumps(p["values"], sort_keys=True),
                       json.dumps(p.get("affects", [])), p["fingerprint"], s))
        elif k == "assignment_run":
            c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                      (p["run_id"], p["branch"], p["nucleus"], p["algo"],
                       p["params_version"], p["params_fp"], json.dumps(p["src_refs"], sort_keys=True),
                       json.dumps(p.get("unmatched", [])), s))
            for cand in p["candidates"]:
                c.execute("INSERT INTO candidates(cand_id,run_id,branch,nucleus,peak_id,atom_id,"
                          "score,rank,tie_group,evidence,notes,status,src_docs,params_fp,event_seq)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (cand["cand_id"], p["run_id"], p["branch"], p["nucleus"],
                           cand["peak_id"], cand["atom_id"], cand.get("score"), cand.get("rank"),
                           cand.get("tie_group"), json.dumps(cand.get("evidence", []), sort_keys=True),
                           json.dumps(cand.get("notes", [])), cand["status"],
                           json.dumps(cand["src_docs"], sort_keys=True), p["params_fp"], s))
        elif k == "candidates_expired":
            for cid in p["cand_ids"]:
                c.execute("UPDATE candidates SET status='expired', expired_seq=?, expire_reason=?"
                          " WHERE cand_id=?", (s, p["reason"], cid))
        elif k == "branch_created":
            c.execute("INSERT INTO branches VALUES(?,?,?,?)",
                      (p["branch_id"], p.get("parent"), p.get("label", ""), s))
        elif k == "decision_made":
            c.execute("INSERT INTO decisions(decision_id,branch,peak_id,atom_id,action,actor,event_seq)"
                      " VALUES(?,?,?,?,?,?,?)",
                      (p["decision_id"], p["branch"], p["peak_id"], p["atom_id"],
                       p["action"], p["actor"], s))
        elif k == "decision_undone":
            c.execute("UPDATE decisions SET undone_seq=? WHERE decision_id=?",
                      (s, p["decision_id"]))
        elif k == "conflict_detected":
            c.execute("INSERT INTO conflicts(conflict_id,branch,peak_id,decision_ids,status,event_seq)"
                      " VALUES(?,?,?,?,?,?)",
                      (p["conflict_id"], p["branch"], p["peak_id"],
                       json.dumps(p["decision_ids"]), "open", s))
        elif k == "conflict_resolved":
            c.execute("UPDATE conflicts SET status='resolved', resolved_seq=?, resolution=?"
                      " WHERE conflict_id=?",
                      (s, json.dumps(p.get("resolution", {})), p["conflict_id"]))
            for did in p.get("superseded", []):
                c.execute("UPDATE decisions SET superseded_seq=? WHERE decision_id=?", (s, did))

    # -- queries -------------------------------------------------------
    def table(self, name: str) -> list[dict]:
        rows = self.conn.execute(f"SELECT * FROM {name}").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for key in ("meta", "values_json", "src_refs", "evidence", "notes",
                        "src_docs", "decision_ids", "affects", "unmatched", "resolution"):
                if key in d and isinstance(d[key], str):
                    d[key] = json.loads(d[key])
            out.append(d)
        return out

    def wipe(self) -> None:
        for t in ("events", "documents", "diagnostics", "params", "runs",
                  "candidates", "branches", "decisions", "conflicts"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()

    def replay(self, events: Iterable[dict]) -> None:
        """Rebuild state from an event list (used by import and time travel)."""
        self.wipe()
        for ev in sorted(events, key=lambda e: e["seq"]):
            self.append(ev["kind"], ev["payload"], ev["actor"], ev["vtime"],
                        branch=ev.get("branch"), seq=ev["seq"], event_id=ev["event_id"])
