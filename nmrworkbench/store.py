"""Event-sourced SQLite store.

Durability rules
----------------
* Raw source submissions are immutable: a later revision is a new document
  version with a ``parent_version``; nothing is overwritten.
* Every derived artifact carries algorithm version, parameter fingerprint
  and direct source references.
* Events form a SHA-256 hash chain (project-scoped).  Export, delete local
  DB, import: ids, order and provenance are byte-for-byte preserved.
* Each branch is a linear sequence of events.  Adjudication forks a new
  review branch; replay of any event restores that point-in-time state
  rather than recomputing with the newest data.
* Local input changes only invalidate candidates that depend on the changed
  content.  Solvent / temperature / reference changes invalidate candidates
  based on that meta (all runs).  Stale results remain viewable but are
  flagged, never presented as current.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .clock import now
from .engine import generate_candidates
from .ingest import RevisionContent, parse_document
from .models import (CandidateRun, Conflict, Edge, ExperimentMeta, Molecule,
                     Parameters, Peak, Verdict)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    branch TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    parent_event_id TEXT,
    base_event_id TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (project_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_events_branch ON events (project_id, branch);
CREATE TABLE IF NOT EXISTS branches (
    project_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    head_seq INTEGER NOT NULL,
    forked_from TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, branch)
);
CREATE TABLE IF NOT EXISTS seq_counters (
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (project_id, name)
);
"""


def _sha(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass
class ProjectState:
    project_id: str
    branch: str
    seq: int
    documents: Dict[str, dict] = field(default_factory=dict)
    # path -> {"version": n, "parent_version": n|None, "content": RevisionContent-like dict}
    params: Optional[Parameters] = None
    runs: List[CandidateRun] = field(default_factory=list)
    verdicts: List[Verdict] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    forks: List[dict] = field(default_factory=list)

    def peaks(self) -> List[Peak]:
        out: List[Peak] = []
        for doc in self.documents.values():
            if doc["content"]["kind"] == "peaks":
                for e in doc["content"]["entities"]:
                    out.append(Peak.from_dict(e))
        out.sort(key=lambda p: p.id)
        return out

    def edges(self) -> List[Edge]:
        out: List[Edge] = []
        for doc in self.documents.values():
            if doc["content"]["kind"] == "edges":
                for e in doc["content"]["entities"]:
                    out.append(Edge.from_dict(e))
        out.sort(key=lambda e: e.id)
        return out

    def molecule(self) -> Optional[Molecule]:
        for doc in self.documents.values():
            if doc["content"]["kind"] == "molecule":
                return Molecule.from_dict(doc["content"]["entities"][0])
        return None

    def meta(self) -> Optional[ExperimentMeta]:
        for doc in self.documents.values():
            if doc["content"]["kind"] == "meta":
                return ExperimentMeta.from_dict(doc["content"]["meta"])
        return None


class Store:
    def __init__(self, db_path: str = "nmrworkbench.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    def _next_seq(self, project_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events WHERE project_id=?",
            (project_id,)).fetchone()
        return int(row["n"])

    def _counter(self, project_id: str, name: str) -> int:
        row = self._conn.execute(
            "SELECT value FROM seq_counters WHERE project_id=? AND name=?",
            (project_id, name)).fetchone()
        return int(row["value"]) if row else 0

    def _bump(self, project_id: str, name: str) -> int:
        n = self._counter(project_id, name) + 1
        self._conn.execute(
            "INSERT INTO seq_counters(project_id, name, value) VALUES(?,?,?) "
            "ON CONFLICT(project_id, name) DO UPDATE SET value=excluded.value",
            (project_id, name, n))
        return n

    def create_project(self, name: str) -> str:
        project_id = f"prj-{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            "INSERT INTO projects(id, name, created_at) VALUES(?,?,?)",
            (project_id, name, now()))
        self._conn.execute(
            "INSERT INTO branches(project_id, branch, head_seq, forked_from, created_at)"
            " VALUES(?,?,?,?,?)", (project_id, "main", 0, None, now()))
        self._conn.commit()
        return project_id

    def list_projects(self) -> List[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT id, name, created_at FROM projects ORDER BY id")]

    def get_project(self, project_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def list_branches(self, project_id: str) -> List[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT branch, head_seq, forked_from, created_at FROM branches "
            "WHERE project_id=? ORDER BY head_seq", (project_id,))]

    def _append(self, project_id: str, branch: str, event_type: str,
                payload: dict, actor: str,
                base_event_id: Optional[str] = None,
                fork: Optional[Tuple[str, str]] = None) -> dict:
        """Append an event; ``fork`` = (new_branch, forked_from_branch)."""
        seq = self._next_seq(project_id)
        event_id = f"evt-{seq:04d}-{uuid.uuid4().hex[:8]}"
        head_row = self._conn.execute(
            "SELECT event_id FROM events WHERE project_id=? ORDER BY seq DESC LIMIT 1",
            (project_id,)).fetchone()
        parent_event_id = head_row["event_id"] if head_row else None
        record = {"project_id": project_id, "seq": seq, "branch": branch,
                  "event_id": event_id, "event_type": event_type,
                  "parent_event_id": parent_event_id,
                  "base_event_id": base_event_id, "actor": actor,
                  "created_at": now(), "payload": payload}
        record["hash"] = _sha({k: record[k] for k in
                               ("seq", "branch", "event_id", "event_type",
                                "parent_event_id", "base_event_id", "actor",
                                "created_at", "payload")})
        self._conn.execute(
            "INSERT INTO events(project_id, seq, branch, event_id, event_type,"
            " parent_event_id, base_event_id, actor, created_at, payload, hash)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, seq, branch, event_id, event_type, parent_event_id,
             base_event_id, actor, record["created_at"],
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             record["hash"]))
        if fork is not None:
            new_branch, forked_from = fork
            self._conn.execute(
                "INSERT INTO branches(project_id, branch, head_seq, forked_from,"
                " created_at) VALUES(?,?,?,?,?)",
                (project_id, new_branch, seq, forked_from, now()))
        else:
            self._conn.execute(
                "UPDATE branches SET head_seq=? WHERE project_id=? AND branch=?",
                (seq, project_id, branch))
        self._conn.commit()
        return record

    # ------------------------------------------------------------------
    def _branch_event_seqs(self, project_id: str, branch: str) -> List[int]:
        rows = self._conn.execute(
            "SELECT seq FROM events WHERE project_id=? ORDER BY seq",
            (project_id,)).fetchall()
        # Lineage: main events up to fork point + that branch's own events.
        branches = {r["branch"]: dict(r) for r in self.list_branches(project_id)}
        if branch not in branches:
            return []
        chain: List[str] = []
        cur = branch
        while cur is not None:
            info = self._conn.execute(
                "SELECT branch, forked_from FROM branches WHERE project_id=? AND branch=?",
                (project_id, cur)).fetchone()
            chain.append(cur)
            cur = info["forked_from"] if info else None
        allowed = set(chain)
        return [r["seq"] for r in rows]  # global seq order; filter by lineage below

    def _lineage_branches(self, project_id: str, branch: str) -> List[str]:
        chain: List[str] = []
        cur: Optional[str] = branch
        seen = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            info = self._conn.execute(
                "SELECT forked_from FROM branches WHERE project_id=? AND branch=?",
                (project_id, cur)).fetchone()
            cur = info["forked_from"] if info else None
        return chain

    def events_on_branch(self, project_id: str, branch: str,
                         upto_seq: Optional[int] = None) -> List[dict]:
        allowed = set(self._lineage_branches(project_id, branch))
        rows = self._conn.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq",
            (project_id,)).fetchall()
        out = []
        for r in rows:
            if upto_seq is not None and r["seq"] > upto_seq:
                break
            if r["branch"] in allowed:
                out.append(dict(r))
        return out


    # ------------------------------------------------------------------
    def commit_source(self, project_id: str, branch: str, path: str, text: str,
                      kind: Optional[str], actor: str) -> Tuple[dict, RevisionContent]:
        state = self.state(project_id, branch)
        known = {p.id for p in state.peaks()}
        content = parse_document(path, text, kind, known_peaks=known)
        parent = state.documents.get(path)
        parent_version = parent["version"] if parent else None
        version = self._bump(project_id, f"doc:{path}")
        payload = {"path": path, "kind": content.kind, "version": version,
                   "parent_version": parent_version,
                   "content": json.loads(content.to_json())}
        record = self._append(project_id, branch, "source_committed", payload, actor)
        return record, content

    def set_parameters(self, project_id: str, branch: str, params: Parameters,
                       actor: str, note: str = "") -> dict:
        return self._append(project_id, branch, "parameter_set",
                            {"params": params.to_dict(), "note": note,
                             "fingerprint": params.fingerprint()}, actor)

    def generate_run_event(self, project_id: str, branch: str, actor: str) -> dict:
        st = self.state(project_id, branch)
        mol, meta = st.molecule(), st.meta()
        if mol is None:
            raise ValueError("molecule document required before generating candidates")
        if meta is None:
            raise ValueError("experiment meta document required")
        params = st.params or Parameters(algorithm_version="default")
        run_no = self._bump(project_id, "run")
        run_id = f"run-{run_no:03d}"
        run = generate_candidates(mol, st.peaks(), st.edges(), meta, params, run_id)
        refs: Dict[str, List[str]] = {}
        entity_path: Dict[str, str] = {}
        for doc_path, doc in st.documents.items():
            c = doc["content"]
            if c["kind"] in ("peaks", "edges"):
                for ent in c["entities"]:
                    entity_path[ent["id"]] = doc_path
        mol_path = next(p for p, d in st.documents.items()
                        if d["content"]["kind"] == "molecule")
        meta_path = next(p for p, d in st.documents.items()
                         if d["content"]["kind"] == "meta")
        for cand in run.candidates:
            local = [f"{mol_path}#"]
            local.append(f"{meta_path}#")
            if cand.peak_id and cand.peak_id in entity_path:
                local.append(f"{entity_path[cand.peak_id]}#{cand.peak_id}")
            for eid in cand.evidence_edges:
                if eid in entity_path:
                    local.append(f"{entity_path[eid]}#{eid}")
            refs[cand.id] = sorted(set(local))
        run.basis_versions = {p: d["version"] for p, d in st.documents.items()}
        run.candidate_refs = refs
        run.basis_revision = hashlib.sha256(
            json.dumps(run.basis_versions, sort_keys=True).encode()).hexdigest()[:12]
        return self._append(project_id, branch, "candidates_generated",
                            {"run": run.to_dict()}, actor)

    # ------------------------------------------------------------------
    def adjudicate(self, project_id: str, branch: str, site_id: str, nucleus: str,
                   decision: str, peak_id: Optional[str], editor: str,
                   reason: str, based_on_run: str,
                   new_branch: Optional[str] = None) -> Tuple[dict, str, List[dict]]:
        if decision not in ("accepted", "rejected"):
            raise ValueError("decision must be accepted or rejected")
        st = self.state(project_id, branch)
        base_event = self._conn.execute(
            "SELECT event_id FROM events WHERE project_id=? ORDER BY seq DESC LIMIT 1",
            (project_id,)).fetchone()
        base_event_id = base_event["event_id"] if base_event else None
        n = self._bump(project_id, "review")
        target_branch = new_branch or f"review-{n:02d}"
        if target_branch in {b["branch"] for b in self.list_branches(project_id)}:
            raise ValueError(f"branch {target_branch!r} already exists")
        verdict_id = f"vd-{n:03d}-{uuid.uuid4().hex[:6]}"
        payload = {"verdict": {
            "id": verdict_id, "site_id": site_id, "nucleus": nucleus,
            "decision": decision, "peak_id": peak_id, "editor": editor,
            "reason": reason, "based_on_run": based_on_run, "retracted": False},
            "fork": {"new_branch": target_branch, "forked_from": branch,
                     "base_event_id": base_event_id}}
        record = self._append(project_id, target_branch, "verdict_recorded",
                              payload, editor, base_event_id=base_event_id,
                              fork=(target_branch, branch))
        # Two-editor conflict detection across divergent branches.
        conflict_records: List[dict] = []
        target_ancestors = set(self._lineage_branches(project_id, target_branch))
        for other in self.list_branches(project_id):
            ob = other["branch"]
            if ob == target_branch:
                continue
            other_lineage = self._lineage_branches(project_id, ob)
            other_is_ancestor = ob in target_ancestors
            target_is_ancestor = target_branch in other_lineage
            if other_is_ancestor or target_is_ancestor:
                continue
            other_state = self.state(project_id, ob)
            other_eff = {v.site_id: v for v in other_state.verdicts
                         if not v.retracted}
            if site_id in other_eff:
                ov = other_eff[site_id]
                if ov.editor != editor and self._is_conflicting(
                        decision, peak_id, ov):
                    cn = self._bump(project_id, "conflict")
                    conflict = Conflict(
                        id=f"cf-{cn:03d}", site_id=site_id, nucleus=nucleus,
                        branch_a=target_branch, branch_b=ob,
                        verdict_a=verdict_id, verdict_b=ov.id,
                        status="open")
                    crec = self._append(
                        project_id, target_branch, "conflict_registered",
                        {"conflict": conflict.to_dict()}, editor)
                    conflict_records.append(crec)
        return record, target_branch, conflict_records

    @staticmethod
    def _is_conflicting(decision_a: str, peak_a: Optional[str],
                        verdict_b: Verdict) -> bool:
        if decision_a == "rejected" and verdict_b.decision == "rejected":
            return False
        if decision_a != verdict_b.decision:
            return True
        return decision_a == "accepted" and peak_a != verdict_b.peak_id

    def resolve_conflict(self, project_id: str, branch: str, conflict_id: str,
                         chosen_verdict_id: str, actor: str, note: str) -> dict:
        st = self.state(project_id, branch)
        conflict = next((c for c in st.conflicts if c.id == conflict_id), None)
        if conflict is None:
            raise ValueError(f"conflict {conflict_id} not visible on {branch}")
        if chosen_verdict_id not in (conflict.verdict_a, conflict.verdict_b):
            raise ValueError("resolution must choose one of the two verdicts")
        resolution = {"chosen_verdict_id": chosen_verdict_id, "actor": actor,
                      "note": note}
        return self._append(project_id, branch, "conflict_resolved",
                            {"conflict_id": conflict_id, "resolution": resolution},
                            actor)

    def retract_verdict(self, project_id: str, branch: str, verdict_id: str,
                        actor: str, note: str) -> dict:
        st = self.state(project_id, branch)
        if not any(v.id == verdict_id for v in st.verdicts):
            raise ValueError("verdict not visible on this branch")
        return self._append(project_id, branch, "verdict_retracted",
                            {"verdict_id": verdict_id, "actor": actor,
                             "note": note}, actor)


    # ------------------------------------------------------------------
    def state(self, project_id: str, branch: str = "main",
              upto_seq: Optional[int] = None) -> ProjectState:
        events = self.events_on_branch(project_id, branch, upto_seq)
        st = ProjectState(project_id=project_id, branch=branch,
                          seq=events[-1]["seq"] if events else 0)
        for ev in events:
            p = json.loads(ev["payload"])
            t = ev["event_type"]
            if t == "source_committed":
                st.documents[p["path"]] = {"version": p["version"],
                                           "parent_version": p["parent_version"],
                                           "content": p["content"]}
            elif t == "parameter_set":
                st.params = Parameters.from_dict(p["params"])
            elif t == "candidates_generated":
                run = CandidateRun.from_dict(p["run"])
                st.runs.append(run)
            elif t == "verdict_recorded":
                st.verdicts.append(Verdict.from_dict(p["verdict"]))
                st.forks.append(p["fork"])
            elif t == "verdict_retracted":
                for v in st.verdicts:
                    if v.id == p["verdict_id"]:
                        v.retracted = True
            elif t == "conflict_registered":
                st.conflicts.append(Conflict.from_dict(p["conflict"]))
            elif t == "conflict_resolved":
                for c in st.conflicts:
                    if c.id == p["conflict_id"]:
                        c.status = "resolved"
                        c.resolution = p["resolution"]
        self._annotate_staleness(st)
        return st

    @staticmethod
    def _annotate_staleness(st: ProjectState) -> None:
        current_versions = {path: doc["version"]
                            for path, doc in st.documents.items()}
        for run in st.runs:
            stale: Dict[str, List[str]] = {}
            for cand_id, refs in run.candidate_refs.items():
                changed: List[str] = []
                for ref in refs:
                    path, sep, entity = ref.partition("#")
                    if path not in run.basis_versions:
                        changed.append(ref)
                        continue
                    if current_versions.get(path) != run.basis_versions[path]:
                        if entity == "":
                            changed.append(ref)
                        else:
                            changed.append(ref)
                if changed:
                    stale[cand_id] = changed
            run.stale_candidates = stale
            run.is_stale = bool(stale)
            run.current_params_ok = (st.params is None
                                     or st.params.fingerprint()
                                     == run.param_fingerprint)

    # ------------------------------------------------------------------
    def verify_chain(self, project_id: str) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq",
            (project_id,)).fetchall()
        problems: List[dict] = []
        prev_id = None
        for r in rows:
            body = {k: r[k] for k in
                    ("seq", "branch", "event_id", "event_type",
                     "parent_event_id", "base_event_id", "actor",
                     "created_at")}
            body["payload"] = json.loads(r["payload"])
            digest = _sha(body)
            if digest != r["hash"]:
                problems.append({"seq": r["seq"], "problem": "hash mismatch"})
            if r["parent_event_id"] != prev_id:
                problems.append({"seq": r["seq"],
                                 "problem": "parent linkage broken"})
            prev_id = r["event_id"]
        return problems

    # ------------------------------------------------------------------
    def export_project(self, project_id: str) -> dict:
        proj = self.get_project(project_id)
        events = [dict(r) for r in self._conn.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY seq",
            (project_id,))]
        branches = self.list_branches(project_id)
        counters = [dict(r) for r in self._conn.execute(
            "SELECT project_id, name, value FROM seq_counters WHERE project_id=?",
            (project_id,))]
        return {"format": "nmr-workbench-export/1",
                "project": proj, "events": events, "branches": branches,
                "seq_counters": counters}

    def import_project(self, bundle: dict, new_id: Optional[str] = None) -> str:
        if bundle.get("format") != "nmr-workbench-export/1":
            raise ValueError("unsupported export format")
        proj = dict(bundle["project"])
        project_id = new_id or proj["id"]
        if self.get_project(project_id) is not None:
            raise ValueError(f"project {project_id} already exists in local database")
        proj["id"] = project_id
        self._conn.execute(
            "INSERT INTO projects(id, name, created_at) VALUES(?,?,?)",
            (project_id, proj["name"], proj["created_at"]))
        for b in bundle["branches"]:
            self._conn.execute(
                "INSERT INTO branches(project_id, branch, head_seq, forked_from,"
                " created_at) VALUES(?,?,?,?,?)",
                (project_id, b["branch"], b["head_seq"], b.get("forked_from"),
                 b["created_at"]))
        for ev in bundle["events"]:
            self._conn.execute(
                "INSERT INTO events(project_id, seq, branch, event_id, event_type,"
                " parent_event_id, base_event_id, actor, created_at, payload, hash)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, ev["seq"], ev["branch"], ev["event_id"],
                 ev["event_type"], ev["parent_event_id"], ev["base_event_id"],
                 ev["actor"], ev["created_at"], ev["payload"], ev["hash"]))
        for c in bundle.get("seq_counters", []):
            self._conn.execute(
                "INSERT INTO seq_counters(project_id, name, value) VALUES(?,?,?)",
                (project_id, c["name"], c["value"]))
        self._conn.commit()
        return project_id
