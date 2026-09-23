"""SQLite-backed immutable event store and deterministic projections."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .engine import domain
from .parser import parse_bundle

SCHEMA_VERSION = "nmr-workbench-storage/1"


def connect(db_path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS branches (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            name TEXT NOT NULL,
            parent_branch_id TEXT REFERENCES branches(id),
            forked_from_event_id TEXT,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            seq INTEGER NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id),
            branch_id TEXT NOT NULL REFERENCES branches(id),
            parent_event_id TEXT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            creator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_events_project_branch ON events(project_id, branch_id, seq);
        """
    )
    conn.commit()


class StoreError(ValueError):
    pass


class ConflictError(StoreError):
    pass


class EventStore:
    def __init__(self, conn: sqlite3.Connection, clock=None):
        self.conn = conn
        self.clock = clock or (lambda: "2026-09-23T00:00:00Z")
        init_db(conn)

    def _now(self) -> str:
        value = self.clock()
        return value if isinstance(value, str) else time.strftime("%Y-%m-%dT%H:%M:%SZ", value)

    def _one(self, sql: str, args: Tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchone()

    def project_exists(self, project_id: str) -> bool:
        return self._one("SELECT id FROM projects WHERE id = ?", (project_id,)) is not None

    def import_bundle(
        self,
        raw_text: str,
        *,
        creator: str = "importer",
        project_id: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        seed: int = 20260923,
    ) -> Dict[str, Any]:
    # The transaction follows parser validation, so malformed input never creates history.
        bundle = parse_bundle(raw_text)
        pid = project_id or bundle["project"]["id"]
        if self.project_exists(pid):
            raise StoreError(f"project already exists: {pid}")
        params = domain.default_params()
        if parameters:
            params = domain.merge_params(params, parameters)
        timestamp = self._now()
        with self.conn:
            self.conn.execute("INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)", (pid, bundle["project"]["name"], timestamp))
            main_id = f"branch:{pid}:main"
            self.conn.execute(
                "INSERT INTO branches(id, project_id, name, parent_branch_id, forked_from_event_id, creator, created_at) VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                (main_id, pid, "main", creator, timestamp),
            )
            e1 = self._insert_event(pid, main_id, None, "project_imported", {"bundle": bundle}, creator, commit=False)
            e2 = self._insert_event(
                pid,
                main_id,
                e1["id"],
                "source_submitted",
                {"version_id": f"source:{pid}:v1", "parent_version_id": None, "documents": bundle["documents"]},
                creator,
                commit=False,
            )
            parent = e2["id"]
            runs = []
            for scope in ("proton", "carbon"):
                run = domain.build_run(scope, bundle["documents"], params, seed=seed)
                event = self._insert_event(pid, main_id, parent, "run_built", {"run": run}, creator, commit=False)
                parent = event["id"]
                runs.append(run)
        return {"project_id": pid, "branch_id": main_id, "head_event_id": parent, "events": [e1, e2], "runs": runs}

    def _next_seq(self, project_id: str) -> int:
        row = self._one("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM events WHERE project_id = ?", (project_id,))
        return int(row["seq"])

    def _insert_event(self, project_id, branch_id, parent_event_id, event_type, payload, creator, commit=True):
        seq = self._next_seq(project_id)
        event_id = f"evt:{project_id}:{seq:04d}"
        self.conn.execute(
            "INSERT INTO events(id, seq, project_id, branch_id, parent_event_id, event_type, payload_json, creator, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                seq,
                project_id,
                branch_id,
                parent_event_id,
                event_type,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                creator,
                self._now(),
            ),
        )
        if commit:
            self.conn.commit()
        return {"id": event_id, "seq": seq, "event_type": event_type, "payload": payload}

    def get_branch(self, branch_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM branches WHERE id = ?", (branch_id,))
        if row is None:
            raise StoreError(f"unknown branch: {branch_id}")
        return row

    def get_project(self, project_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if row is None:
            raise StoreError(f"unknown project: {project_id}")
        return row

    def fork_branch(self, project_id: str, from_event_id: str, name: str, creator: str) -> Dict[str, Any]:
        source_event = self._event_by_id(project_id, from_event_id)
        source_branch = self.get_branch(source_event["branch_id"])
        seq = self._next_seq(project_id)
        branch_id = f"branch:{project_id}:r{seq:04d}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO branches(id, project_id, name, parent_branch_id, forked_from_event_id, creator, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (branch_id, project_id, name, source_branch["id"], from_event_id, creator, self._now()),
            )
        return {"id": branch_id, "parent_branch_id": source_branch["id"], "forked_from_event_id": from_event_id}

    def list_branches(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM branches WHERE project_id = ? ORDER BY rowid", (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def _event_by_id(self, project_id: str, event_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM events WHERE project_id = ? AND id = ?", (project_id, event_id))
        if row is None:
            raise StoreError(f"unknown event: {event_id}")
        return row

    def _branch_head(self, branch_id: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM events WHERE branch_id = ? ORDER BY seq DESC LIMIT 1", (branch_id,))

    def lineage_events(self, branch_id: str, *, at_event_id: Optional[str] = None) -> List[sqlite3.Row]:
        branch = self.get_branch(branch_id)
        events: List[sqlite3.Row] = []
        if branch["parent_branch_id"]:
            parent_events = self.lineage_events(branch["parent_branch_id"])
            cutoff = self._event_by_id(branch["project_id"], branch["forked_from_event_id"])
            events.extend([e for e in parent_events if e["seq"] <= cutoff["seq"]])
        own = self.conn.execute(
            "SELECT * FROM events WHERE branch_id = ? ORDER BY seq", (branch_id,)
        ).fetchall()
        events.extend(own)
        events.sort(key=lambda e: e["seq"])
        if at_event_id:
            target = self._event_by_id(branch["project_id"], at_event_id)
            allowed = {e["id"] for e in events}
            if at_event_id not in allowed:
                raise StoreError("event is not visible from this branch")
            events = [e for e in events if e["seq"] <= target["seq"]]
        return events

    def append_run(self, project_id: str, branch_id: str, scope: str, documents: List[Dict[str, Any]], parameters: Dict[str, Any], creator: str, reason: str) -> Dict[str, Any]:
        head = self._branch_head(branch_id)
        run = domain.build_run(scope, documents, parameters)
        with self.conn:
            event = self._insert_event(
                project_id,
                branch_id,
                head["id"] if head else None,
                "run_built",
                {"run": run, "reason": reason},
                creator,
                commit=False,
            )
        return event

    def revise_parameters(
        self,
        project_id: str,
        branch_id: str,
        patch: Dict[str, Any],
        creator: str,
        *,
        reason: str = "parameter revision",
        scopes: Iterable[str] = ("proton", "carbon"),
        documents: Optional[List[Dict[str, Any]]] = None,
        seed: int = 20260923,
    ) -> List[Dict[str, Any]]:
        projection = self.project_view(project_id, branch_id)
        params = domain.merge_params(projection["parameters"], patch)
        docs = documents if documents is not None else projection["documents"]
        head = self._branch_head(branch_id)
        made = []
        with self.conn:
            revision = self._insert_event(
                project_id,
                branch_id,
                head["id"] if head else None,
                "parameters_revised",
                {"patch": patch, "parameters": params, "reason": reason, "fingerprint": domain.fingerprint(params, "params")},
                creator,
                commit=False,
            )
            made.append(revision)
            parent_id = revision["id"]
            for scope in scopes:
                run = domain.build_run(scope, docs, params, seed=seed)
                run_event = self._insert_event(
                    project_id,
                    branch_id,
                    parent_id,
                    "run_built",
                    {"run": run, "reason": reason},
                    creator,
                    commit=False,
                )
                parent_id = run_event["id"]
                made.append(run_event)
        return made

    def revise_source(
        self,
        project_id: str,
        branch_id: str,
        replacement_document: Dict[str, Any],
        creator: str,
        *,
        reason: str,
        seed: int = 20260923,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        projection = self.project_view(project_id, branch_id)
        old_docs = projection["documents"]
        doc_id = replacement_document["id"]
        if not any(doc["id"] == doc_id for doc in old_docs):
            raise StoreError("replacement source id is not present")
        new_docs = [doc if doc["id"] != doc_id else replacement_document for doc in old_docs]
        old_deps = domain.run_dependencies(old_docs)
        new_deps = domain.run_dependencies(new_docs)
        affected = domain.affected_scopes(old_deps, new_deps)
        params = parameters or projection["parameters"]
        head = self._branch_head(branch_id)
        made = []
        with self.conn:
            submit = self._insert_event(
                project_id,
                branch_id,
                head["id"] if head else None,
                "source_submitted",
                {
                    "version_id": f"source:{project_id}:v{projection['source_version'] + 1}",
                    "parent_version_id": projection["source_version_id"],
                    "replaced_document_id": doc_id,
                    "documents": [replacement_document],
                    "reason": reason,
                },
                creator,
                commit=False,
            )
            made.append(submit)
            parent_id = submit["id"]
            if not affected:
                expire = self._insert_event(
                    project_id,
                    branch_id,
                    parent_id,
                    "no_dependency_invalidated",
                    {"changed_document_id": doc_id, "reason": reason},
                    creator,
                    commit=False,
                )
                made.append(expire)
            else:
                for scope in affected:
                    expiry = self._insert_event(
                        project_id,
                        branch_id,
                        parent_id,
                        "candidates_invalidated",
                        {
                            "scope": scope,
                            "affected_run_ids": [projection["runs"][scope]["id"]],
                            "reason": "direct source fingerprint changed",
                            "old_dependencies": old_deps[scope],
                            "new_dependencies": new_deps[scope],
                        },
                        creator,
                        commit=False,
                    )
                    parent_id = expiry["id"]
                    made.append(expiry)
                    run = domain.build_run(scope, new_docs, params, seed=seed)
                    run_event = self._insert_event(
                        project_id,
                        branch_id,
                        parent_id,
                        "run_built",
                        {"run": run, "reason": reason},
                        creator,
                        commit=False,
                    )
                    parent_id = run_event["id"]
                    made.append(run_event)
        return made

    def decide_candidate(
        self,
        project_id: str,
        branch_id: str,
        candidate_id: str,
        verdict: str,
        creator: str,
        *,
        rationale: str,
        new_branch_name: str,
        propagate: bool = True,
    ) -> Dict[str, Any]:
        if verdict not in {"accepted", "rejected", "uncertain"}:
            raise StoreError("verdict must be accepted, rejected or uncertain")
        projection = self.project_view(project_id, branch_id)
        found = None
        for scope, run in projection["runs"].items():
            for candidate in run.get("candidates", []):
                if candidate["id"] == candidate_id:
                    found = (scope, run, candidate)
        if found is None:
            raise StoreError("candidate is not visible in current branch runs")
        scope, run, candidate = found
        forked = self.fork_branch(project_id, projection["head_event_id"], new_branch_name, creator)
        new_branch = forked["id"]
        tied_ids = sorted(
            c["id"]
            for c in run["candidates"]
            if candidate.get("tie_group") and c.get("tie_group") == candidate.get("tie_group")
        )
        overlap_group = next((g for g in run.get("overlap_groups", []) if candidate["peak_id"] in g["peak_ids"]), None)
        decision_id = "decision:" + domain.fingerprint(
            {"candidate": candidate_id, "verdict": verdict, "creator": creator, "rationale": rationale, "parent": projection["head_event_id"]},
            "dec",
        )
        payload = {
            "decision_id": decision_id,
            "candidate_id": candidate_id,
            "scope": scope,
            "verdict": verdict,
            "creator": creator,
            "rationale": rationale,
            "retains_all_legal_candidates": True,
            "forced_unique_assignment": False,
            "tied_candidate_ids": tied_ids,
            "overlap_group_id": overlap_group["id"] if overlap_group else None,
            "propagation": {
                "applies_to_run_id": run["id"],
                "does_not_mutate_run": True,
                "stale_only_after_dependency_change": True,
            },
        }
        with self.conn:
            event = self._insert_event(
                project_id,
                new_branch,
                projection["head_event_id"],
                "candidate_decided",
                payload,
                creator,
                commit=False,
            )
        return {"branch": forked, "event": event}

    def record_decision_conflict(self, project_id: str, branch_id: str, left_event_id: str, right_event_id: str, creator: str) -> Dict[str, Any]:
        left = self._event_by_id(project_id, left_event_id)
        right = self._event_by_id(project_id, right_event_id)
        lp = json.loads(left["payload_json"])
        rp = json.loads(right["payload_json"])
        if left["event_type"] != "candidate_decided" or right["event_type"] != "candidate_decided":
            raise ConflictError("both events must be human decisions")
        if lp["candidate_id"] != rp["candidate_id"]:
            raise ConflictError("decisions concern different candidates")
        if lp["verdict"] == rp["verdict"]:
            raise ConflictError("decisions do not conflict")
        head = self._branch_head(branch_id)
        conflict_id = "conflict:" + domain.fingerprint(
            {"left": left_event_id, "right": right_event_id, "candidate": lp["candidate_id"]}, "cf"
        )
        payload = {
            "conflict_id": conflict_id,
            "state": "open",
            "candidate_id": lp["candidate_id"],
            "left_event_id": left_event_id,
            "right_event_id": right_event_id,
            "left_verdict": lp["verdict"],
            "right_verdict": rp["verdict"],
            "left_creator": lp["creator"],
            "right_creator": rp["creator"],
            "resolution": None,
        }
        with self.conn:
            event = self._insert_event(project_id, branch_id, head["id"] if head else None, "decision_conflict_detected", payload, creator, commit=False)
        return event

    def resolve_decision_conflict(self, project_id: str, branch_id: str, conflict_id: str, resolution: str, creator: str, rationale: str) -> Dict[str, Any]:
        if resolution not in {"accept_left", "accept_right", "keep_unresolved"}:
            raise StoreError("invalid conflict resolution")
        head = self._branch_head(branch_id)
        payload = {
            "conflict_id": conflict_id,
            "state": "resolved" if resolution != "keep_unresolved" else "open",
            "resolution": resolution,
            "creator": creator,
            "rationale": rationale,
            "historical_events_retained": True,
            "forced_unique_assignment": False,
        }
        with self.conn:
            return self._insert_event(project_id, branch_id, head["id"] if head else None, "decision_conflict_resolved", payload, creator, commit=False)

    def undo_decision(self, project_id: str, branch_id: str, decision_id: str, creator: str, rationale: str) -> Dict[str, Any]:
        head = self._branch_head(branch_id)
        payload = {"decision_id": decision_id, "creator": creator, "rationale": rationale, "restores_candidate_state": "eligible"}
        with self.conn:
            return self._insert_event(project_id, branch_id, head["id"] if head else None, "decision_reverted", payload, creator, commit=False)

    def project_view(self, project_id: str, branch_id: str, *, at_event_id: Optional[str] = None) -> Dict[str, Any]:
        project = self.get_project(project_id)
        branch = self.get_branch(branch_id)
        rows = self.lineage_events(branch_id, at_event_id=at_event_id)
        events = []
        documents: List[Dict[str, Any]] = []
        source_versions = []
        parameters = domain.default_params()
        runs: Dict[str, Dict[str, Any]] = {}
        run_history: List[Dict[str, Any]] = []
        decisions = []
        conflicts = []
        active_decisions: Dict[str, Dict[str, Any]] = {}
        conflict_state: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            event = {
                "id": row["id"],
                "seq": row["seq"],
                "branch_id": row["branch_id"],
                "parent_event_id": row["parent_event_id"],
                "type": row["event_type"],
                "creator": row["creator"],
                "created_at": row["created_at"],
                "payload": payload,
            }
            events.append(event)
            etype = row["event_type"]
            if etype == "project_imported":
                continue
            if etype == "source_submitted":
                if payload.get("parent_version_id") is None:
                    documents = list(payload["documents"])
                else:
                    replacements = {doc["id"]: doc for doc in payload["documents"]}
                    documents = [replacements.get(doc["id"], doc) for doc in documents]
                source_versions.append(
                    {
                        "version_id": payload["version_id"],
                        "parent_version_id": payload.get("parent_version_id"),
                        "event_id": row["id"],
                        "document_ids": [doc["id"] for doc in payload["documents"]],
                    }
                )
            elif etype == "parameters_revised":
                parameters = payload["parameters"]
            elif etype == "run_built":
                run = payload["run"]
                scope = run["scope"]
                if scope in runs:
                    runs[scope]["state"] = "superseded"
                fresh = json.loads(json.dumps(run))
                fresh["state"] = "current"
                fresh["event_id"] = row["id"]
                runs[scope] = fresh
                run_history.append(fresh)
            elif etype == "candidate_decided":
                decisions.append(event)
                active_decisions[payload["candidate_id"]] = event
            elif etype == "decision_reverted":
                active_decisions.pop(payload["decision_id"], None)
                decisions.append(event)
            elif etype == "decision_conflict_detected":
                conflicts.append(event)
                conflict_state[payload["conflict_id"]] = event
            elif etype == "decision_conflict_resolved":
                previous = conflict_state.get(payload["conflict_id"], {}).get("payload", {}).copy()
                previous.update(payload)
                merged_event = {"id": row["id"], "seq": row["seq"], "type": "decision_conflict_resolved", "payload": previous}
                conflict_state[payload["conflict_id"]] = merged_event
                conflicts.append(event)
        final_events = self.lineage_events(branch_id)
        final_documents = self._latest_documents(final_events)
        final_deps = domain.run_dependencies(final_documents) if final_documents else {}
        for scope, run in runs.items():
            run["decisions"] = []
            stale = not domain.dependencies_match(run["dependencies"], final_deps.get(scope, run["dependencies"]))
            run["view_state"] = "stale" if stale else "current"
            for candidate in run.get("candidates", []):
                decision = active_decisions.get(candidate["id"])
                if decision and decision["payload"].get("propagation", {}).get("applies_to_run_id") == run["id"]:
                    candidate["status"] = decision["payload"]["verdict"]
                    candidate["decision_event_id"] = decision["id"]
                    candidate["decision_creator"] = decision["payload"]["creator"]
                    run["decisions"].append({"candidate_id": candidate["id"], "event_id": decision["id"], "verdict": candidate["status"]})
        skeleton = next((doc["parsed"] for doc in documents if doc["kind"] == "molecular_skeleton"), None)
        return {
            "schema_version": SCHEMA_VERSION,
            "project": {"id": project["id"], "name": project["name"], "created_at": project["created_at"]},
            "branch": dict(branch),
            "head_event_id": events[-1]["id"] if events else None,
            "events": events,
            "documents": documents,
            "source_versions": source_versions,
            "parameters": parameters,
            "parameter_fingerprint": domain.fingerprint(parameters, "params"),
            "runs": runs,
            "run_history": run_history,
            "decisions": decisions,
            "conflicts": list(conflict_state.values()),
            "skeleton": skeleton,
            "seed": next((r["seed"] for r in runs.values()), 20260923),
        }

    def _latest_documents(self, rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
        documents: List[Dict[str, Any]] = []
        for row in rows:
            if row["event_type"] != "source_submitted":
                continue
            payload = json.loads(row["payload_json"])
            if payload.get("parent_version_id") is None:
                documents = list(payload["documents"])
            else:
                replacements = {doc["id"]: doc for doc in payload["documents"]}
                documents = [replacements.get(doc["id"], doc) for doc in documents]
        return documents

    def export_project(self, project_id: str) -> Dict[str, Any]:
        self.get_project(project_id)
        projects = [dict(r) for r in self.conn.execute("SELECT * FROM projects WHERE id = ? ORDER BY id", (project_id,))]
        branches = [dict(r) for r in self.conn.execute("SELECT * FROM branches WHERE project_id = ? ORDER BY rowid", (project_id,))]
        events = [dict(r) for r in self.conn.execute("SELECT * FROM events WHERE project_id = ? ORDER BY seq", (project_id,))]
        return {"schema_version": SCHEMA_VERSION, "exported_at": self._now(), "projects": projects, "branches": branches, "events": events}

    def import_project_export(self, export: Dict[str, Any]) -> str:
        if export.get("schema_version") != SCHEMA_VERSION:
            raise StoreError("unsupported export schema")
        if not export.get("projects"):
            raise StoreError("export has no project")
        pid = export["projects"][0]["id"]
        if self.project_exists(pid):
            raise StoreError(f"project already exists: {pid}")
        with self.conn:
            for row in export["projects"]:
                self.conn.execute("INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)", (row["id"], row["name"], row["created_at"]))
            for row in export["branches"]:
                self.conn.execute(
                    "INSERT INTO branches(id, project_id, name, parent_branch_id, forked_from_event_id, creator, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], row["project_id"], row["name"], row["parent_branch_id"], row["forked_from_event_id"], row["creator"], row["created_at"]),
                )
            for row in export["events"]:
                self.conn.execute(
                    "INSERT INTO events(id, seq, project_id, branch_id, parent_event_id, event_type, payload_json, creator, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], row["seq"], row["project_id"], row["branch_id"], row["parent_event_id"], row["event_type"], row["payload_json"], row["creator"], row["created_at"]),
                )
        return pid
