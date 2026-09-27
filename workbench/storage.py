"""SQLite-backed event store with branch projection and replay.

Events are append-only and content-addressed (each hash commits to its parent).
Derived snapshots are stored immutable and marked *stale* at read time when any
direct source version they depend on has advanced on the viewed branch.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .engine import derive_candidates

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    parent_id TEXT,
    second_parent_id TEXT,
    branch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (project_id, event_id)
);
CREATE TABLE IF NOT EXISTS branches (
    project_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    label TEXT NOT NULL,
    head_event_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, branch_id)
);
CREATE TABLE IF NOT EXISTS sources (
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT,
    raw_text TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    submitter TEXT NOT NULL,
    parent_version INTEGER,
    PRIMARY KEY (project_id, source_id, version)
);
CREATE TABLE IF NOT EXISTS runs (
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, run_id)
);
"""


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(payload)).hexdigest()


class Clock:
    def __init__(self, start: float = 1_790_000_000.0, step: float = 1.0):
        self.t = start
        self.step = step

    def now(self) -> str:
        self.t += self.step
        return f"{self.t:.3f}"

    def now_float(self) -> float:
        self.t += self.step
        return self.t


class Store:
    def __init__(self, db_path: str = ":memory:", clock: Callable[[], str] | None = None):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._clock = clock or Clock().now

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ basic
    def create_project(self, title: str, project_id: str | None = None,
                       author: str = "reviewer") -> dict[str, Any]:
        project_id = project_id or "PRJ-" + uuid.uuid4().hex[:12]
        created = self._clock()
        with self.transaction() as conn:
            conn.execute("INSERT INTO projects VALUES (?,?,?)", (project_id, title, created))
            conn.execute("INSERT INTO branches VALUES (?,?,?,?,?)",
                         (project_id, "main", "main", None, created))
            event = self._append(conn, project_id, "main", None, "project_created",
                                 {"title": title}, author)
            conn.execute("UPDATE branches SET head_event_id=? WHERE project_id=? AND branch_id='main'",
                         (event["event_id"], project_id))
        return {"project_id": project_id, "root_event_id": event["event_id"]}

    def _append(self, conn: sqlite3.Connection, project_id: str, branch_id: str,
                parent_id: str | None, kind: str, payload: dict[str, Any], author: str,
                second_parent_id: str | None = None) -> dict[str, Any]:
        n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE project_id=?", (project_id,)).fetchone()["n"]
        seq = n + 1
        payload_hash = sha(payload)
        header = {"seq": seq, "project_id": project_id, "parent_id": parent_id,
                  "second_parent_id": second_parent_id, "kind": kind, "author": author,
                  "payload_hash": payload_hash}
        event_hash = "EVT-" + hashlib.sha256(canonical(header)).hexdigest()[:12]
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (event_hash, project_id, seq, parent_id, second_parent_id, branch_id,
                      kind, self._clock(), author, json.dumps(payload, ensure_ascii=False),
                      payload_hash))
        return {"event_id": event_hash, "seq": seq, "payload_hash": payload_hash}

    def append_event(self, project_id: str, branch_id: str, kind: str,
                     payload: dict[str, Any], author: str = "reviewer") -> dict[str, Any]:
        with self.transaction() as conn:
            head = conn.execute("SELECT head_event_id FROM branches WHERE project_id=? AND branch_id=?",
                                (project_id, branch_id)).fetchone()
            if head is None:
                raise KeyError(f"unknown branch {branch_id}")
            event = self._append(conn, project_id, branch_id, head["head_event_id"], kind, payload, author)
            conn.execute("UPDATE branches SET head_event_id=? WHERE project_id=? AND branch_id=?",
                         (event["event_id"], project_id, branch_id))
        return event

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def get_project(self, project_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(project_id)
        return dict(row)

    def list_branches(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM branches WHERE project_id=? ORDER BY created_at",
                                 (project_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- sources
    def submit_source(self, project_id: str, branch_id: str, kind: str, raw_text: str,
                      parsed: dict[str, Any], filename: str | None = None,
                      source_id: str | None = None, author: str = "importer") -> dict[str, Any]:
        """Submit a new immutable source version; supersedes same-kind on branch."""
        with self.transaction() as conn:
            head = conn.execute("SELECT head_event_id FROM branches WHERE project_id=? AND branch_id=?",
                                (project_id, branch_id)).fetchone()["head_event_id"]
            prev = conn.execute(
                "SELECT source_id, version FROM sources WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project_id,)).fetchone()
            prev_same = conn.execute(
                """SELECT source_id, version FROM sources s
                   JOIN events e ON 1=1
                   WHERE s.project_id=? AND s.kind=?
                   ORDER BY s.version DESC LIMIT 1""",
                (project_id, kind)).fetchone()
            if source_id is None:
                source_id = f"SRC-{kind}-" + uuid.uuid4().hex[:8]
            maxv = conn.execute("SELECT COALESCE(MAX(version),0) AS m FROM sources WHERE project_id=?",
                                (project_id,)).fetchone()["m"]
            version = maxv + 1
            parent_version = prev_same["version"] if prev_same else None
            if prev_same and prev_same["source_id"] != source_id:
                source_id = prev_same["source_id"]
            ts = self._clock()
            conn.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (project_id, source_id, version, kind, filename, raw_text,
                          json.dumps(parsed, ensure_ascii=False), ts, author, parent_version))
            payload = {"source_id": source_id, "version": version, "kind": kind,
                       "filename": filename, "parent_version": parent_version,
                       "diagnostics": parsed.get("diagnostics", [])}
            event = self._append(conn, project_id, branch_id, head, "source_submitted", payload, author)
            conn.execute("UPDATE branches SET head_event_id=? WHERE project_id=? AND branch_id=?",
                         (event["event_id"], project_id, branch_id))
        return {"event_id": event["event_id"], "source_id": source_id, "version": version,
                "parent_version": parent_version}

    def get_source_version(self, project_id: str, source_id: str, version: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM sources WHERE project_id=? AND source_id=? AND version=?",
                                (project_id, source_id, version)).fetchone()
        if not row:
            raise KeyError((source_id, version))
        d = dict(row)
        d["parsed"] = json.loads(d["parsed_json"])
        return d

    # -------------------------------------------------------------- projection
    def head_id(self, project_id: str, branch_id: str) -> str:
        row = self.conn.execute("SELECT head_event_id FROM branches WHERE project_id=? AND branch_id=?",
                                (project_id, branch_id)).fetchone()
        if not row:
            raise KeyError(branch_id)
        return row["head_event_id"]

    def events_ancestors(self, project_id: str, head_event_id: str) -> list[dict[str, Any]]:
        rows = {r["event_id"]: r for r in
                self.conn.execute("SELECT * FROM events WHERE project_id=?", (project_id,)).fetchall()}
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()

        def visit(eid: str | None) -> None:
            if not eid or eid in seen:
                return
            row = rows.get(eid)
            if row is None:
                return
            seen.add(eid)
            visit(row["parent_id"])
            visit(row["second_parent_id"])
            ordered.append(dict(row))

        visit(head_event_id)
        ordered.sort(key=lambda r: r["seq"])
        return ordered

    def branch_view(self, project_id: str, branch_id: str, at_event_id: str | None = None) -> dict[str, Any]:
        head = at_event_id or self.head_id(project_id, branch_id)
        events = self.events_ancestors(project_id, head)
        current: dict[str, dict[str, Any]] = {}
        for ev in events:
            if ev["kind"] == "source_submitted":
                p = json.loads(ev["payload"])
                current[p["kind"]] = {"source_id": p["source_id"], "version": p["version"],
                                      "event_id": ev["event_id"]}
        visible_runs = []
        for ev in events:
            if ev["kind"] == "derived":
                p = json.loads(ev["payload"])
                run = self.get_run(project_id, p["run_id"])
                visible_runs.append(self._run_with_staleness(project_id, run, current))
        latest_run = visible_runs[-1] if visible_runs else None
        decisions = [self._decision_dict(ev) for ev in events if ev["kind"] == "decision"]
        resolutions = [self._resolution_dict(ev) for ev in events if ev["kind"] == "conflict_resolved"]
        return {
            "project_id": project_id, "branch_id": branch_id, "head_event_id": head,
            "events": [self._event_dict(e) for e in events],
            "current_sources": current,
            "latest_run": latest_run,
            "runs": visible_runs,
            "decisions": decisions,
            "conflict_resolutions": resolutions,
        }

    def _event_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"event_id": row["event_id"], "seq": row["seq"], "parent_id": row["parent_id"],
                "second_parent_id": row["second_parent_id"], "branch_id": row["branch_id"],
                "kind": row["kind"], "created_at": row["created_at"], "author": row["author"],
                "payload_hash": row["payload_hash"], "payload": json.loads(row["payload"])}

    def _decision_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        d = self._event_dict(row)
        d["payload"]["superseded"] = False
        return d

    def _resolution_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        return self._event_dict(row)

    # ----------------------------------------------------------------- derive
    REQUIRED_KINDS = ("context", "skeleton", "params", "h1_peaks", "c13_peaks",
                      "cosy_edges", "hsqc_edges", "hmbc_edges")

    def _build_bundle(self, project_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        latest: dict[str, dict[str, Any]] = {}
        for ev in events:
            if ev["kind"] == "source_submitted":
                latest[json.loads(ev["payload"])["kind"]] = json.loads(ev["payload"])
        missing = [k for k in self.REQUIRED_KINDS if k not in latest]
        if missing:
            raise ValueError(f"missing source kinds before derivation: {missing}")
        bundle = {}
        for kind, ref in latest.items():
            src = self.get_source_version(project_id, ref["source_id"], ref["version"])
            bundle[kind] = {"source_id": ref["source_id"], "version": ref["version"],
                            "parsed": src["parsed"], "raw_text": src["raw_text"]}
        return bundle

    def run_derivation(self, project_id: str, branch_id: str, label: str = "",
                       author: str = "system") -> dict[str, Any]:
        head = self.head_id(project_id, branch_id)
        events = self.events_ancestors(project_id, head)
        bundle = self._build_bundle(project_id, events)
        result = derive_candidates(bundle)
        result["label"] = label
        n = self.conn.execute("SELECT COUNT(*) AS n FROM runs WHERE project_id=?",
                              (project_id,)).fetchone()["n"]
        run_id = f"RUN-{n + 1:04d}"
        with self.transaction() as conn:
            event = self._append(conn, project_id, branch_id, head, "derived",
                                 {"run_id": run_id, "label": label,
                                  "parameter_fingerprint": result["parameter_fingerprint"],
                                  "source_versions": result["source_versions"],
                                  "algorithm": result["algorithm"],
                                  "algorithm_version": result["algorithm_version"]},
                                 author)
            conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                         (project_id, run_id, event["event_id"], branch_id,
                          json.dumps(result, ensure_ascii=False), self._clock()))
            conn.execute("UPDATE branches SET head_event_id=? WHERE project_id=? AND branch_id=?",
                         (event["event_id"], project_id, branch_id))
        return self.get_run(project_id, run_id) | {"event_id": event["event_id"]}

    def get_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM runs WHERE project_id=? AND run_id=?",
                                (project_id, run_id)).fetchone()
        if not row:
            raise KeyError(run_id)
        d = dict(row)
        d["result"] = json.loads(d["result_json"])
        return d

    def _run_with_staleness(self, project_id: str, run: dict[str, Any],
                            current: dict[str, dict[str, Any]]) -> dict[str, Any]:
        result = run["result"]
        stale_components: list[dict[str, Any]] = []
        for kind, ref in result.get("source_versions", {}).items():
            cur = current.get(kind)
            if cur and (cur["source_id"] != ref["source_id"] or cur["version"] != ref["version"]):
                affected = self._affected_components(kind)
                stale_components.append({"source_kind": kind,
                                         "snapshot_version": ref["version"],
                                         "current_version": cur["version"],
                                         "affected_components": affected,
                                         "reason": self._stale_reason(kind)})
        out = {"run_id": run["run_id"], "event_id": run["event_id"], "branch_id": run["branch_id"],
               "created_at": run["created_at"], "is_current": not stale_components,
               "stale_components": stale_components, "result": result}
        out["candidates"] = self._candidate_view(result, stale_components)
        return out

    @staticmethod
    def _affected_components(kind: str) -> list[str]:
        return {
            "h1_peaks": ["proton-shift", "integral", "multiplicity"],
            "c13_peaks": ["carbon-shift"],
            "cosy_edges": ["network:COSY"],
            "hsqc_edges": ["network:HSQC"],
            "hmbc_edges": ["network:HMBC"],
            "params": ["all-components"],
            "skeleton": ["all-components"],
            "context": ["context-dependent-validity"],
        }.get(kind, ["unknown"])

    @staticmethod
    def _stale_reason(kind: str) -> str:
        if kind == "context":
            return ("solvent / temperature / reference-standard changed: dependent candidates "
                    "invalidated, prior decisions remain replayable")
        if kind.endswith("_edges"):
            return "correlation input changed: only network evidence component invalidated"
        return "direct source advanced on branch"

    @staticmethod
    def _candidate_view(result: dict[str, Any], stale_components: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stale_kinds = {s["source_kind"] for s in stale_components}
        view = []
        for cand in result["candidates"]:
            comps = set()
            if cand["nucleus"] == "1H" and "h1_peaks" in stale_kinds:
                comps.update(["proton-shift", "integral", "multiplicity"])
            if cand["nucleus"] == "13C" and "c13_peaks" in stale_kinds:
                comps.add("carbon-shift")
            for exp in ("cosy_edges", "hsqc_edges", "hmbc_edges"):
                if exp in stale_kinds:
                    comps.add("network:" + exp.split("_")[0].upper())
            if "params" in stale_kinds or "skeleton" in stale_kinds:
                comps.add("all-components")
            if "context" in stale_kinds:
                comps.add("context-dependent-validity")
            c = dict(cand)
            c["stale"] = bool(comps)
            c["stale_components"] = sorted(comps)
            c["current_valid"] = not bool(comps)
            view.append(c)
        return view

    # -------------------------------------------------------------- decisions
    def record_decision(self, project_id: str, branch_id: str, candidate_id: str,
                        verdict: str, rationale: str = "", author: str = "reviewer",
                        run_id: str | None = None) -> dict[str, Any]:
        if verdict not in ("accepted", "rejected"):
            raise ValueError("verdict must be accepted or rejected")
        view = self.branch_view(project_id, branch_id)
        run = view["latest_run"]
        if run is None:
            raise ValueError("no derivation on branch yet")
        cand = next((c for c in run["candidates"] if c["candidate_id"] == candidate_id), None)
        if cand is None:
            raise KeyError(candidate_id)
        previous = [d for d in view["decisions"]
                    if d["payload"]["candidate_id"] == candidate_id and d["payload"]["verdict"] == verdict
                    or (d["payload"].get("candidate_id") == candidate_id)]
        payload = {
            "candidate_id": candidate_id, "verdict": verdict, "rationale": rationale,
            "site_id": cand["site_id"], "nucleus": cand["nucleus"], "peak_id": cand.get("peak_id"),
            "run_id": run["run_id"], "run_event_id": run["event_id"],
            "parameter_fingerprint": run["result"]["parameter_fingerprint"],
            "direct_sources": run["result"]["source_versions"],
            "supersedes": [d["event_id"] for d in view["decisions"]
                           if d["payload"].get("candidate_id") == candidate_id],
            "impact": self._decision_impact(cand, run),
        }
        return self.append_event(project_id, branch_id, "decision", payload, author)

    @staticmethod
    def _decision_impact(cand: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        tied = cand.get("tie_with", [])
        siblings = [c["candidate_id"] for c in run["candidates"]
                    if c["site_id"] == cand["site_id"] and c["candidate_id"] != cand["candidate_id"]]
        return {"tied_candidates": tied, "other_candidates_for_same_nucleus": siblings,
                "note": "accepting/rejecting creates a new branch point; ranking is not overwritten"}

    # --------------------------------------------------------------- branches
    def create_branch(self, project_id: str, label: str, from_event_id: str | None = None,
                      author: str = "reviewer", branch_id: str | None = None) -> dict[str, Any]:
        head = from_event_id or self.head_id(project_id, "main")
        branch_id = branch_id or "BR-" + uuid.uuid4().hex[:8]
        created = self._clock()
        with self.transaction() as conn:
            conn.execute("INSERT INTO branches VALUES (?,?,?,?,?)",
                         (project_id, branch_id, label, head, created))
            event = self._append(conn, branch_id, head, "branch_created",
                                 {"branch_id": branch_id, "label": label, "from_event_id": head}, author)
            conn.execute("UPDATE branches SET head_event_id=? WHERE project_id=? AND branch_id=?",
                         (event["event_id"], project_id, branch_id))
        return {"branch_id": branch_id, "event_id": event["event_id"], "head_event_id": head}

    # ------------------------------------------------------------- merge/undo
    def detect_conflicts(self, project_id: str, branch_a: str, branch_b: str,
                         base_event_id: str | None = None) -> list[dict[str, Any]]:
        base_events = {e["event_id"] for e in
                       self.events_ancestors(project_id, base_event_id or self.head_id(project_id, "main"))}
        dec_a = [e for e in self.events_ancestors(project_id, self.head_id(project_id, branch_a))
                 if e["kind"] == "decision" and e["event_id"] not in base_events]
        dec_b = [e for e in self.events_ancestors(project_id, self.head_id(project_id, branch_b))
                 if e["kind"] == "decision" and e["event_id"] not in base_events]
        by_cand_a = {json.loads(e["payload"])["candidate_id"]: e for e in dec_a}
        by_cand_b = {json.loads(e["payload"])["candidate_id"]: e for e in dec_b}
        conflicts = []
        for cid in sorted(set(by_cand_a) & set(by_cand_b)):
            ea, eb = by_cand_a[cid], by_cand_b[cid]
            pa, pb = json.loads(ea["payload"]), json.loads(eb["payload"])
            conflicts.append({
                "candidate_id": cid,
                "branch_a": branch_a, "decision_a": pa["verdict"], "event_a": ea["event_id"],
                "author_a": ea["author"], "rationale_a": pa.get("rationale", ""),
                "branch_b": branch_b, "decision_b": pb["verdict"], "event_b": eb["event_id"],
                "author_b": eb["author"], "rationale_b": pb.get("rationale", ""),
                "resolvable": True,
            })
        return conflicts

    def merge_branches(self, project_id: str, target_branch: str, source_branch: str,
                       resolution: str, resolutions: dict[str, str] | None = None,
                       author: str = "lead-reviewer") -> dict[str, Any]:
        """Merge source branch into target.

        ``resolution`` is 'take-target' | 'take-source' | 'manual'.  Conflicting
        verdicts are never auto-silenced; a conflict_resolved event records the
        chosen verdict per candidate and both parent decisions stay in history.
        """
        conflicts = self.detect_conflicts(project_id, target_branch, source_branch)
        target_head = self.head_id(project_id, target_branch)
        source_head = self.head_id(project_id, source_branch)
        payload = {"target_branch": target_branch, "source_branch": source_branch,
                   "resolution": resolution, "conflicts": conflicts, "resolved": []}
        for conf in conflicts:
            if resolution == "take-target":
                chosen = conf["decision_a"]
            elif resolution == "take-source":
                chosen = conf["decision_b"]
            else:
                chosen = (resolutions or {}).get(conf["candidate_id"], conf["decision_a"])
            payload["resolved"].append({"candidate_id": conf["candidate_id"],
                                        "chosen_verdict": chosen,
                                        "losing_event": conf["event_b"] if chosen == conf["decision_a"]
                                        else conf["event_a"],
                                        "kept_event": conf["event_a"] if chosen == conf["decision_a"]
                                        else conf["event_b"]})
        with self.transaction() as conn:
            event = self._append(conn, project_id, target_branch, target_head, "conflict_resolved",
                                 payload, author, second_parent_id=source_head)
            conn.execute("UPDATE branches SET head_event_id=? WHERE project_id=? AND branch_id=?",
                         (event["event_id"], project_id, target_branch))
        return event

    def undo_last(self, project_id: str, branch_id: str, author: str = "reviewer") -> dict[str, Any]:
        """Logical undo: appends a reversal event; nothing is deleted."""
        view = self.branch_view(project_id, branch_id)
        last = next((e for e in reversed(view["events"])
                     if e["kind"] in ("decision", "conflict_resolved", "parameter_change")), None)
        if last is None:
            raise ValueError("no reversible event on branch")
        return self.append_event(project_id, branch_id, "undone",
                                 {"reverted_event_id": last["event_id"],
                                  "reverted_kind": last["kind"]}, author)

    # --------------------------------------------------------- export/import
    def export_project(self, project_id: str) -> dict[str, Any]:
        proj = self.get_project(project_id)
        events = [dict(r) for r in self.conn.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()]
        for e in events:
            e["payload"] = e.pop("payload")
        sources = [dict(r) for r in self.conn.execute(
            "SELECT * FROM sources WHERE project_id=? ORDER BY version, kind", (project_id,)).fetchall()]
        runs = [dict(r) for r in self.conn.execute(
            "SELECT * FROM runs WHERE project_id=? ORDER BY created_at, run_id", (project_id,)).fetchall()]
        branches = [dict(r) for r in self.conn.execute(
            "SELECT * FROM branches WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()]
        return {"format": "nmr-workbench-export/1", "project": proj,
                "branches": branches, "events": events, "sources": sources, "runs": runs}

    def import_project(self, data: dict[str, Any]) -> str:
        if data.get("format") != "nmr-workbench-export/1":
            raise ValueError("unsupported export format")
        proj = data["project"]
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO projects VALUES (?,?,?)",
                         (proj["project_id"], proj["title"], proj["created_at"]))
            for b in data["branches"]:
                conn.execute("INSERT OR REPLACE INTO branches VALUES (?,?,?,?,?)",
                             (b["project_id"], b["branch_id"], b["label"],
                              b["head_event_id"], b["created_at"]))
            for s in data["sources"]:
                parsed = s["parsed_json"]
                if not isinstance(parsed, str):
                    parsed = json.dumps(parsed, ensure_ascii=False)
                conn.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (s["project_id"], s["source_id"], s["version"], s["kind"],
                              s.get("filename"), s["raw_text"], parsed,
                              s["submitted_at"], s["submitter"], s.get("parent_version")))
            for r in data["runs"]:
                result = r["result_json"]
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                             (r["project_id"], r["run_id"], r["event_id"], r["branch_id"],
                              result, r["created_at"]))
            for e in data["events"]:
                conn.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (e["event_id"], e["project_id"], e["seq"], e.get("parent_id"),
                              e.get("second_parent_id"), e["branch_id"], e["kind"],
                              e["created_at"], e["author"],
                              e["payload"] if isinstance(e["payload"], str)
                              else json.dumps(e["payload"], ensure_ascii=False),
                              e["payload_hash"]))
        return proj["project_id"]

    def close(self) -> None:
        self.conn.close()
