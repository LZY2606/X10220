"""Application service: replayable projections over the event log.

History rule: returning to an old event restores the exact source versions,
parameters and visible conclusions at that point; later computations never
overwrite history. Decisions therefore snapshot their candidate and the
parameter/condition fingerprints; a later solvent/temperature/reference/source
change only *expires* affected decisions (they remain visible and replayable).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from .importer import parse_document, SourceDocument
from .engine import compute_analysis, DEFAULT_PARAMS, ALGORITHM_VERSION
from .fingerprint import fingerprint
from .store import EventStore


@dataclass
class ProjectState:
    project_id: str
    branch: str = "main"
    seq: int = -1
    documents: Dict[str, SourceDocument] = field(default_factory=dict)  # logical id -> current
    document_history: Dict[str, List[SourceDocument]] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_PARAMS))
    decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    analysis: Optional[Dict[str, Any]] = None
    expired: Dict[str, List[str]] = field(default_factory=dict)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)


def _key(doc: SourceDocument):
    return doc.doc_id  # logical identity across versions


def replay(store: EventStore, project_id, branch="main", upto_seq=None):
    state = ProjectState(project_id=project_id, branch=branch)
    events = store.events_for_branch(project_id, branch, upto_seq)
    state.seq = events[-1]["seq"] if events else -1
    for ev in events:
        p = ev["payload"]
        kind = ev["kind"]
        if kind == "source_submitted":
            doc = _doc_from_payload(p)
            if not doc.accepted:
                # rejected documents are recorded but do not enter the analysis
                state.document_history.setdefault(_key(doc), []).append(doc)
                continue
            state.documents[_key(doc)] = doc
            state.document_history.setdefault(_key(doc), []).append(doc)
        elif kind == "parameters_set":
            state.params = {**state.params, **p["parameters"]}
        elif kind == "candidate_decided":
            state.decisions[p["nucleus_id"]] = {
                "event_id": ev["event_id"], "seq": ev["seq"],
                "author": ev["author"], "ts": ev["ts"],
                "branch": branch, **p}
        elif kind == "conflict_resolved":
            state.decisions[p["nucleus_id"]] = {
                "event_id": ev["event_id"], "seq": ev["seq"],
                "author": ev["author"], "ts": ev["ts"],
                "branch": branch, **p}
    return state


def _doc_from_payload(p):
    return parse_document(
        p["raw_text"], p.get("filename", p["logical_id"] + ".nmr"),
        doc_id=p["logical_id"], version=p["version"],
        parent_version=p.get("parent_version"))


def _snapshot_candidate(analysis, nucleus_id, peak_id):
    for c in analysis["candidates"].get(nucleus_id, []):
        if c["peak_id"] == peak_id:
            return {
                "peak_id": c["peak_id"], "score": c["score"],
                "shift_residual": c["shift_residual"],
                "status": c["status"], "rank": c["rank"],
                "tied_best": c["tied_best"],
                "supported_edges": c["supported_edges"],
                "ambiguity_group": c.get("ambiguity_group"),
                "symmetry_group": c.get("symmetry_group"),
            }
    return None


def _find_conflicts(store, project_id, nucleus_id, verdict, peak_id,
                    base_branch="main"):
    """A same-nucleus decision with a different outcome on main is a conflict."""
    base = replay(store, project_id, base_branch)
    prior = base.decisions.get(nucleus_id)
    if prior and (prior.get("verdict") != verdict or
                  prior.get("peak_id") != peak_id):
        return prior
    return None


class WorkbenchService:
    def __init__(self, store: EventStore):
        self.store = store

    # -- mutations ---------------------------------------------------------
    def create_project(self, project_id, name, now=None):
        return self.store.create_project(project_id, name, now)

    def submit_source(self, project_id, logical_id, raw_text, filename,
                      author="importer", branch="main", parent_event=None,
                      claimed_version=None):
        existing = replay(self.store, project_id, branch).documents.get(logical_id)
        version = claimed_version
        if version is None:
            version = str(int(existing.version) + 1) if existing else "1"
        payload = {
            "logical_id": logical_id, "raw_text": raw_text, "filename": filename,
            "version": version,
            "parent_version": existing.version if existing else None,
        }
        doc = _doc_from_payload(payload)
        ev = self.store.append(project_id, "source_submitted", payload,
                               author=author, branch=branch, parent_id=parent_event)
        return ev, doc

    def set_parameters(self, project_id, parameters, reason="", author="reviewer",
                       branch="main", parent_event=None):
        payload = {"parameters": parameters, "reason": reason}
        return self.store.append(project_id, "parameters_set", payload,
                                 author=author, branch=branch,
                                 parent_id=parent_event)

    def decide(self, project_id, nucleus_id, verdict, peak_id=None, reason="",
               author="reviewer", branch="main", parent_event=None,
               allow_conflict_branch=True):
        """Record confirm/reject. Conflicting same-nucleus decision forks a branch.

        Returns (event, new_branch_or_None, conflict_prior_or_None).
        """
        if verdict not in ("accepted", "rejected"):
            raise ValueError("verdict must be accepted|rejected")
        state = replay(self.store, project_id, branch,
                       None if parent_event is None else
                       self.store.get_event(parent_event)["seq"])
        analysis = analyze_state(state)["analysis"]
        snap = _snapshot_candidate(analysis, nucleus_id, peak_id)
        prior_conflict = _find_conflicts(self.store, project_id, nucleus_id,
                                         verdict, peak_id)
        payload = {
            "nucleus_id": nucleus_id, "verdict": verdict, "peak_id": peak_id,
            "reason": reason,
            "candidate_snapshot": snap,
            "algorithm_version": ALGORITHM_VERSION,
            "parameter_fingerprint": analysis["parameter_fingerprint"],
            "condition_fingerprint": analysis["condition_fingerprint"],
            "source_versions": {s["doc_id"]: s["version"]
                                for s in analysis["sources"]},
        }
        target_branch = branch
        if prior_conflict and branch == "main" and allow_conflict_branch:
            target_branch = (f"conflict/{nucleus_id}/"
                             f"ev{self.store.next_seq(project_id)}")
        ev = self.store.append(project_id, "candidate_decided", payload,
                               author=author, branch=target_branch,
                               parent_id=parent_event)
        return ev, (target_branch if target_branch != branch else None), \
            prior_conflict

    def resolve_conflict(self, project_id, nucleus_id, verdict, peak_id,
                         resolution="kept_second_editor", winner_event=None,
                         loser_event=None, author="lead-reviewer",
                         branch="main"):
        state = replay(self.store, project_id, branch)
        analysis = analyze_state(state)["analysis"]
        payload = {
            "nucleus_id": nucleus_id, "verdict": verdict, "peak_id": peak_id,
            "resolution": resolution, "winner_event": winner_event,
            "loser_event": loser_event,
            "candidate_snapshot": _snapshot_candidate(analysis, nucleus_id, peak_id),
            "parameter_fingerprint": analysis["parameter_fingerprint"],
            "condition_fingerprint": analysis["condition_fingerprint"],
            "source_versions": {s["doc_id"]: s["version"]
                                for s in analysis["sources"]},
        }
        return self.store.append(project_id, "conflict_resolved", payload,
                                 author=author, branch=branch)

    def add_note(self, project_id, note, author="reviewer", branch="main"):
        return self.store.append(project_id, "note_added", {"note": note},
                                 author=author, branch=branch)

    # -- reads -------------------------------------------------------------
    def view(self, project_id, branch="main", upto_seq=None):
        state = replay(self.store, project_id, branch, upto_seq)
        return analyze_state(state, store=self.store, project_id=project_id)


def analyze_state(state: ProjectState, store=None, project_id=None):
    docs = list(state.documents.values())
    analysis = compute_analysis(docs, state.params) if docs else None
    out = {
        "project_id": project_id or state.project_id,
        "branch": state.branch, "seq": state.seq,
        "algorithm_version": ALGORITHM_VERSION,
        "parameters": state.params,
        "sources": analysis["sources"] if analysis else [],
        "document_versions": {
            k: [{"doc_id": d.doc_id, "version": d.version,
                 "parent_version": d.parent_version,
                 "content_hash": d.content_hash, "accepted": d.accepted,
                 "filename": d.filename,
                 "diagnostics": [x.to_json() for x in d.diagnostics]}
                for d in hist]
            for k, hist in state.document_history.items()},
        "analysis": analysis,
        "decisions": {},
        "expired_decisions": [],
    }
    if analysis:
        cur_pfp = analysis["parameter_fingerprint"]
        cur_cfp = analysis["condition_fingerprint"]
        cur_src = {s["doc_id"]: s["content_hash"] for s in analysis["sources"]}
        for nid, dec in state.decisions.items():
            reasons = []
            if dec.get("parameter_fingerprint") != cur_pfp:
                reasons.append("parameters_changed")
            if dec.get("condition_fingerprint") != cur_cfp:
                reasons.append("condition_changed")
            used = dec.get("source_versions", {})
            # content-level staleness: any used source replaced by new content
            for sid, ver in used.items():
                cur = state.documents.get(sid)
                if cur and cur.version != ver:
                    reasons.append(f"source_revised:{sid}")
            enriched = dict(dec)
            enriched["expired_reasons"] = sorted(set(reasons))
            if reasons:
                enriched["currently_valid"] = False
                out["expired_decisions"].append(enriched)
            else:
                enriched["currently_valid"] = True
            out["decisions"][nid] = enriched
        out["live_decision_count"] = sum(
            1 for d in out["decisions"].values() if d["currently_valid"])
    out["branches"] = store.branches(project_id or state.project_id) if store else {}
    out["conflicts"] = detect_all_conflicts(
        store, project_id or state.project_id) if store else []
    return out


def detect_all_conflicts(store, project_id):
    branches = store.branches(project_id)
    main = replay(store, project_id, "main")
    conflicts = []
    for br in branches:
        if br == "main":
            continue
        other = replay(store, project_id, br)
        for nid, dec in other.decisions.items():
            m = main.decisions.get(nid)
            if m and (m["verdict"] != dec["verdict"] or
                      m.get("peak_id") != dec.get("peak_id")):
                conflicts.append({
                    "nucleus_id": nid,
                    "main_event": m["event_id"],
                    "branch": br,
                    "branch_event": dec["event_id"],
                    "main_verdict": [m["verdict"], m.get("peak_id")],
                    "branch_verdict": [dec["verdict"], dec.get("peak_id")],
                    "authors": [m["author"], dec["author"]],
                    "resolved": any(
                        e["kind"] == "conflict_resolved"
                        and e["payload"].get("nucleus_id") == nid
                        for e in store.events_for_branch(project_id, "main")),
                })
    return conflicts
