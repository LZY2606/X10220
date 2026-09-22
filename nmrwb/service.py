"""Application service: orchestrates parser + engine + event store.

The state machine implemented here:

  submit source -> immutable version + event -> compute run on the branch
  revise source -> new version (parent linked) -> only dependent candidates
                   expire; the old run stays viewable but is marked stale
  accept/reject  -> new child branch + decision event + propagation snapshot
  retract        -> decision_retracted event on the same branch
  conflict       -> two editors acting from the same base event is recorded as
                    a decision_conflict event; resolving it appends
                    conflict_resolved instead of erasing either side
  parameter edit -> parameter_adjusted event + recompute under a new rules
                    version; old run remains replayable
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from . import db
from .parser import parse_source, FormatError
from .engine import compute, run_signature, _param_fp, DEFAULT_PARAMS


class ServiceError(ValueError):
    pass


class ProjectService:
    def __init__(self, conn, clock: Optional[db.Clock] = None):
        self.conn = conn
        self.clock = clock or db.Clock()

    # ------------------------------------------------------------------
    def create_project(self, project_id: str, name: str) -> dict:
        db.create_project(self.conn, project_id, name, self.clock)
        return {"project_id": project_id, "name": name}

    def submit_source(self, project_id: str, source_id: str, kind: str,
                      filename: str, raw_text: str, actor: str = "importer",
                      branch_id: str = "B:main") -> dict:
        existing = self.conn.execute(
            "SELECT version_id, raw_text FROM sources WHERE project_id=? "
            "AND source_id=? ORDER BY rowid DESC LIMIT 1",
            (project_id, source_id)).fetchone()
        if existing is not None:
            raise ServiceError(
                f"source {source_id!r} already exists; use revise_source()")
        version_id = "v1"
        return self._commit_source(project_id, source_id, version_id, None,
                                   kind, filename, raw_text, actor, branch_id,
                                   "source_submitted")

    def revise_source(self, project_id: str, source_id: str, raw_text: str,
                      actor: str, reason: str, branch_id: str = "B:main") -> dict:
        prev = self.conn.execute(
            "SELECT version_id, kind, filename, raw_text FROM sources "
            "WHERE project_id=? AND source_id=? ORDER BY rowid DESC LIMIT 1",
            (project_id, source_id)).fetchone()
        if prev is None:
            raise ServiceError(f"unknown source {source_id!r}")
        if db.sha(raw_text) == prev["raw_text"] and False:
            pass
        nxt_num = int(prev["version_id"].lstrip("v")) + 1
        version_id = f"v{nxt_num}"
        # Parse both to validate and to compute local impact.
        parsed_new = parse_source(raw_text, source_id, version_id)
        parsed_old = parse_source(prev["raw_text"], source_id, prev["version_id"])
        if parsed_new.kind != prev["kind"]:
            raise ServiceError("cannot change source kind in a revision")
        changed_records = _diff_records(parsed_old, parsed_new)
        return self._commit_source(project_id, source_id, version_id,
                                   prev["version_id"], prev["kind"],
                                   prev["filename"], raw_text, actor, branch_id,
                                   "source_revision",
                                   extra={"reason": reason,
                                          "changed_records": changed_records})

    def _commit_source(self, project_id, source_id, version_id, parent_version,
                       kind, filename, raw_text, actor, branch_id, event_kind,
                       extra=None) -> dict:
        parsed = parse_source(raw_text, source_id, version_id)
        if parsed.kind != kind:
            raise FormatError(f"header kind {parsed.kind!r} != declared {kind!r}")
        ts = db._iso(self.clock.now())
        db.store_source(self.conn, project_id, source_id, version_id,
                        parent_version, kind, filename, raw_text, ts)
        payload = {"source_id": source_id, "version_id": version_id,
                   "parent_version_id": parent_version, "kind": kind,
                   "filename": filename,
                   "content_hash": db.sha(raw_text)}
        if extra:
            payload.update(extra)
        event = db.add_event(self.conn, project_id, branch_id, event_kind,
                             payload, actor, self.clock)
        run = self._recompute(project_id, branch_id, actor,
                              cause_event_id=event["event_id"],
                              changed=(source_id, payload.get("changed_records")))
        self.conn.commit()
        return {"event": event, "parsed": parsed.to_dict(), "run": run}

    # ------------------------------------------------------------------
    def _active_sources(self, project_id: str, branch_id: str
                        ) -> Tuple[Dict[str, Tuple[str, object]], Dict[str, str]]:
        snap = db.project_snapshot(self.conn, project_id, branch_id)
        active = snap["active_versions"]
        assembled: Dict[str, Tuple[str, object]] = {}
        for sid, vid in active.items():
            row = db.get_source(self.conn, project_id, sid, vid)
            parsed = parse_source(row["raw_text"], sid, vid)
            assembled[sid] = (vid, parsed)
        return assembled, active

    def _recompute(self, project_id: str, branch_id: str, actor: str,
                   cause_event_id: Optional[str] = None,
                   changed: Optional[Tuple[str, list]] = None) -> Optional[dict]:
        assembled, active = self._active_sources(project_id, branch_id)
        kinds = {sid: ps.kind for sid, (_v, ps) in assembled.items()}
        if not ("skeleton" in set(kinds.values()) and "rules" in set(kinds.values())):
            return None
        if not any(k in ("1h", "13c") for k in kinds.values()):
            return None
        result = compute(assembled)
        sv = {sid: v for sid, (v, _p) in assembled.items()}
        run_id = "R:" + run_signature(sv, result.params)
        existing = self.conn.execute(
            "SELECT 1 FROM runs WHERE project_id=? AND branch_id=? AND run_id=?",
            (project_id, branch_id, run_id)).fetchone()
        created_at = db._iso(self.clock.now())
        if existing is None:
            db.store_run(self.conn, project_id, branch_id, run_id, sv,
                         result.param_fingerprint, result.to_dict(),
                         cause_event_id or "E:manual", created_at)
            event = db.add_event(self.conn, project_id, branch_id, "run_computed",
                                 {"run_id": run_id,
                                  "param_fingerprint": result.param_fingerprint,
                                  "inputs_fingerprint": result.inputs_fingerprint,
                                  "source_versions": sv,
                                  "caused_by": cause_event_id},
                                 actor, self.clock)
            result_doc = result.to_dict()
            result_doc["run_id"] = run_id
            result_doc["event_id"] = event["event_id"]
        else:
            event = None
            result_doc = result.to_dict()
            result_doc["run_id"] = run_id

        # Local expiry: attach expiry events tied to the changed source records.
        if changed is not None:
            self._emit_expiry(project_id, branch_id, actor, changed,
                              result_doc, cause_event_id)
            self.conn.commit()
        result_doc["stale"] = False
        return result_doc

    def _emit_expiry(self, project_id, branch_id, actor, changed, result_doc,
                     cause_event_id):
        source_id, changed_records = changed
        if changed_records is None:
            return
        affected_candidates, affected_nuclei, scope = self._impact_scope(
            source_id, changed_records, result_doc)
        if scope == "global":
            payload = {"scope": "global", "source_id": source_id,
                      "caused_by": cause_event_id,
                      "affected_candidates": sorted(affected_candidates),
                      "affected_nuclei": sorted(affected_nuclei),
                      "message": "rules/skeleton/meta change invalidates all "
                                 "dependent candidates; old run remains viewable"}
        else:
            payload = {"scope": "local", "source_id": source_id,
                      "changed_records": changed_records,
                      "caused_by": cause_event_id,
                      "affected_candidates": sorted(affected_candidates),
                      "affected_nuclei": sorted(affected_nuclei),
                      "message": "only candidates depending on changed records "
                                 "expire; unrelated decisions stay current"}
        db.add_event(self.conn, project_id, branch_id, "candidate_expired",
                     payload, "system", self.clock,
                     base_event_id=cause_event_id)

    # ------------------------------------------------------------------
    def _impact_scope(self, source_id: str, changed_records: list,
                      result_doc: dict):
        """Compute which candidates/nuclei are touched by a source change."""
        affected_candidates: set = set()
        affected_nuclei: set = set()
        global_change = False
        for rec in changed_records:
            rtype = rec["type"]
            rid = rec["id"]
            if rtype == "meta":
                # solvent / temperature / reference / frequency changes only
                # expire candidates from that 1D source (scoped, not global).
                global_change = False
                for nuc in result_doc["nuclei"]:
                    for c in nuc["candidates"]:
                        if c["peak_id"].startswith(f"{source_id}/"):
                            affected_candidates.add(c["id"])
                            affected_nuclei.add(nuc["nucleus"])
                continue
            if rtype in ("atom", "bond", "param", "prior"):
                # skeleton/rules changes propagate broadly to dependent results
                global_change = True
                continue
            # peak / edge record change -> candidates that depend on it
            for nuc in result_doc["nuclei"]:
                for c in nuc["candidates"]:
                    dep_src = {d.split("/")[0] for d in c["depends_on"]}
                    hit = False
                    if rtype == "peak":
                        hit = f"P:{rid}" in c["peak_id"] and source_id in dep_src
                    elif rtype == "edge":
                        hit = (source_id in dep_src and
                               any(rtype == "edge" and
                                   rid in (e.get("source_ref", "") +
                                           json.dumps(e))
                                   for e in c["evidence"]))
                        if not hit:
                            hit = source_id in dep_src and _edge_evidence_mentions(
                                c, source_id, rid)
                    if hit:
                        affected_candidates.add(c["id"])
                        affected_nuclei.add(nuc["nucleus"])
        if global_change:
            for nuc in result_doc["nuclei"]:
                affected_nuclei.add(nuc["nucleus"])
                for c in nuc["candidates"]:
                    affected_candidates.add(c["id"])
        scope = "global" if global_change else "local"
        return affected_candidates, affected_nuclei, scope

    # ------------------------------------------------------------------
    def decide(self, project_id: str, branch_id: str, candidate_id: str,
               action: str, actor: str, reason: str = "",
               expected_base_event_id: Optional[str] = None,
               create_child_branch: bool = True) -> dict:
        """Accept or reject a candidate.

        ``expected_base_event_id`` implements optimistic concurrency: when two
        editors decide from the same base but disagree, the second one is
        recorded as a ``decision_conflict`` (and resolved explicitly), never
        silently overwriting the first decision.
        """
        assert action in ("accept", "reject")
        snap = db.project_snapshot(self.conn, project_id, branch_id)
        cand, nucleus = self._find_candidate(snap, candidate_id)
        if cand is None:
            raise ServiceError(f"candidate {candidate_id!r} not found on branch "
                               f"{branch_id!r}")
        base_event_id = snap["events"][-1]["event_id"] if snap["events"] else None
        prior_decisions = _decisions_for(snap["decisions"], candidate_id)

        conflict = None
        if expected_base_event_id and prior_decisions:
            last = prior_decisions[-1]
            if last["kind"] in ("candidate_accepted", "candidate_rejected"):
                prior_action = ("accept" if last["kind"] == "candidate_accepted"
                                else "reject")
                if prior_action != action:
                    conflict = {"other_actor": last["actor"],
                                "other_event_id": last["event_id"],
                                "other_action": prior_action,
                                "expected_base_event_id": expected_base_event_id,
                                "actual_base_event_id": last["event_id"]}

        target_branch = branch_id
        if create_child_branch:
            target_branch = self._spawn_decision_branch(
                project_id, branch_id, base_event_id, candidate_id, action)

        kind = "candidate_accepted" if action == "accept" else "candidate_rejected"
        payload = {"candidate_id": candidate_id, "nucleus": nucleus,
                   "action": action, "reason": reason,
                   "peak_id": cand["peak_id"], "score": cand["score"],
                   "param_fingerprint": snap["current_run"]["param_fingerprint"]
                   if snap["current_run"] else None,
                   "run_id": snap["current_run"]["run_id"]
                   if snap["current_run"] else None,
                   "on_branch": branch_id}
        if conflict:
            payload["conflict"] = conflict
        propagation = self._propagation(snap, candidate_id, action)
        payload["propagation"] = propagation
        event = db.add_event(self.conn, project_id, target_branch, kind, payload,
                             actor, self.clock, base_event_id=base_event_id)

        conflict_event = None
        if conflict:
            conflict_event = db.add_event(
                self.conn, project_id, target_branch, "decision_conflict",
                {"candidate_id": candidate_id,
                 "winner_pending_resolution": True,
                 "events": [event["event_id"], conflict["other_event_id"]],
                 "actors": [actor, conflict["other_actor"]],
                 "message": "two editors reached different decisions from the "
                            "same base; both retained, resolution required"},
                "system", self.clock, base_event_id=event["event_id"])
        self.conn.commit()
        return {"event": event, "branch_id": target_branch,
                "propagation": propagation, "conflict_event": conflict_event}

    def _spawn_decision_branch(self, project_id, parent_branch, base_event_id,
                               candidate_id, action) -> str:
        existing = [b["branch_id"] for b in db.list_branches(self.conn, project_id)]
        n = sum(1 for b in existing if b.startswith(f"{parent_branch}/D:"))
        branch_id = f"{parent_branch}/D:{n + 1}:{candidate_id.replace(':', '_')}"
        label = f"{action} {candidate_id}"
        db.create_branch(self.conn, project_id, branch_id, parent_branch,
                         base_event_id, label, self.clock)
        db.add_event(self.conn, project_id, branch_id, "branch_created",
                     {"branch_id": branch_id, "parent_branch_id": parent_branch,
                      "base_event_id": base_event_id, "label": label,
                      "candidate_id": candidate_id},
                      "system", self.clock, base_event_id=base_event_id)
        return branch_id

    def resolve_conflict(self, project_id: str, branch_id: str,
                         candidate_id: str, resolution: str, actor: str,
                         reason: str = "") -> dict:
        assert resolution in ("accept", "reject", "keep_both", "retract")
        event = db.add_event(
            self.conn, project_id, branch_id, "conflict_resolved",
            {"candidate_id": candidate_id, "resolution": resolution,
             "reason": reason,
             "message": ("conflict resolution appended; prior conflicting events "
                         "remain in history and replay")},
            actor, self.clock)
        self.conn.commit()
        return event

    def retract_decision(self, project_id: str, branch_id: str,
                         candidate_id: str, actor: str, reason: str = "") -> dict:
        event = db.add_event(
            self.conn, project_id, branch_id, "decision_retracted",
            {"candidate_id": candidate_id, "reason": reason,
             "message": "decision retracted; retraction itself is history"},
            actor, self.clock)
        self.conn.commit()
        return event

    def adjust_parameters(self, project_id: str, branch_id: str, new_rules_text: str,
                          actor: str, reason: str) -> dict:
        """A parameter revision is a new version of the rules source."""
        sid = self._rules_source_id(project_id)
        return self.revise_source(project_id, sid, new_rules_text, actor,
                                  reason, branch_id=branch_id)

    # ------------------------------------------------------------------
    def _rules_source_id(self, project_id: str) -> str:
        for s in db.list_sources(self.conn, project_id):
            if s["kind"] == "rules":
                return s["source_id"]
        raise ServiceError("no rules source present")

    def _find_candidate(self, snap: dict, candidate_id: str):
        run = snap.get("current_run")
        if run is None:
            return None, None
        for nuc in run["nuclei"]:
            for c in nuc["candidates"]:
                if c["id"] == candidate_id:
                    return c, nuc["nucleus"]
        return None, None

    def _propagation(self, snap: dict, candidate_id: str, action: str) -> dict:
        """What else changes if this candidate is accepted/rejected.

        Accepting locks nucleus->peak; this invalidates competing candidates of
        the same nucleus and conflicts with other nuclei claiming the same peak
        (which matters for overlapping / symmetric peaks).  Nothing here mutates
        input peaks: it is a read-only evidence/ranking propagation.
        """
        run = snap.get("current_run")
        if run is None:
            return {"blocked_candidates": [], "competing_nuclei": [],
                    "evidence_rerank": []}
        target = None
        target_nuc = None
        for nuc in run["nuclei"]:
            for c in nuc["candidates"]:
                if c["id"] == candidate_id:
                    target = c
                    target_nuc = nuc["nucleus"]
        if target is None:
            return {"blocked_candidates": [], "competing_nuclei": [],
                    "evidence_rerank": []}
        blocked = []
        competing = []
        for nuc in run["nuclei"]:
            for c in nuc["candidates"]:
                if c["id"] == candidate_id:
                    continue
                if c["peak_id"] == target["peak_id"] and nuc["nucleus"] != target_nuc:
                    competing.append({"candidate_id": c["id"],
                                      "nucleus": nuc["nucleus"],
                                      "reason": "shares the same peak"})
                if nuc["nucleus"] == target_nuc and action == "accept":
                    blocked.append({"candidate_id": c["id"],
                                    "reason": "same nucleus; alternative candidate"})
        affected_edges = [e["code"] for e in target.get("evidence", [])]
        return {
            "action": action,
            "locked": {"nucleus": target_nuc, "peak_id": target["peak_id"]},
            "blocked_candidates": blocked,
            "competing_nuclei": competing,
            "evidence_codes": affected_edges,
            "note": ("propagation is advisory on overlapping/symmetric peaks; "
                     "legal alternatives are retained, not deleted"),
        }

    # ------------------------------------------------------------------
    def export_bundle(self, project_id: str) -> dict:
        return db.export_bundle(self.conn, project_id)

    def snapshot(self, project_id: str, branch_id: str = "B:main",
                 stop_event_id: Optional[str] = None) -> dict:
        return db.project_snapshot(self.conn, project_id, branch_id,
                                   stop_event_id)

    def branches(self, project_id: str) -> List[dict]:
        return db.list_branches(self.conn, project_id)

    def events(self, project_id: str) -> List[dict]:
        return db.list_events(self.conn, project_id)

    def runs(self, project_id: str) -> List[dict]:
        return db.list_runs(self.conn, project_id)

    def sources(self, project_id: str) -> List[dict]:
        return db.list_sources(self.conn, project_id)


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------

def _decisions_for(decisions: List[dict], candidate_id: str) -> List[dict]:
    out = []
    for d in decisions:
        if d.get("candidate_id") == candidate_id and d["kind"] in (
                "candidate_accepted", "candidate_rejected"):
            out.append(d)
    return out


def _edge_evidence_mentions(cand: dict, source_id: str, eid: str) -> bool:
    needle = f"{source_id}/"
    for e in cand.get("evidence", []):
        ref = e.get("source_ref", "")
        if ref.startswith(needle) and (eid in e.get("message", "") or
                                       f"E:{eid}" in e.get("message", "")):
            return True
    return False


def _diff_records(old, new) -> List[dict]:
    """Structural diff of two parsed versions of the same source.

    Returns record-level ids so expiry is scoped to the candidates that truly
    depend on the changed rows; meta changes (solvent/temp/reference/frequency)
    are reported as ``meta`` records.
    """
    changes: List[dict] = []

    def meta_hash(ps):
        return sorted(ps.meta.items())

    if meta_hash(old) != meta_hash(new):
        changed_keys = sorted({k for k, _ in meta_hash(old)} ^
                              {k for k, _ in meta_hash(new)})
        if not changed_keys:
            changed_keys = sorted(set(old.meta) | set(new.meta))
        changes.append({"type": "meta", "id": "meta",
                        "changed_keys": changed_keys})

    def peak_sig(p):
        return [p.pid, p.shift.raw, p.intensity.raw, p.integral.raw,
                p.multiplicity]

    def edge_sig(e):
        return [e.eid, e.dim1, e.dim2, e.intensity.raw]

    old_p = {p.pid: peak_sig(p) for p in old.peaks}
    new_p = {p.pid: peak_sig(p) for p in new.peaks}
    for pid in sorted(set(old_p) | set(new_p)):
        if old_p.get(pid) != new_p.get(pid):
            changes.append({"type": "peak", "id": pid,
                            "present_before": pid in old_p,
                            "present_after": pid in new_p})
    old_e = {e.eid: edge_sig(e) for e in old.edges}
    new_e = {e.eid: edge_sig(e) for e in new.edges}
    for eid in sorted(set(old_e) | set(new_e)):
        if old_e.get(eid) != new_e.get(eid):
            changes.append({"type": "edge", "id": eid,
                            "present_before": eid in old_e,
                            "present_after": eid in new_e})
    old_a = {a.label: a.to_dict() for a in old.atoms}
    new_a = {a.label: a.to_dict() for a in new.atoms}
    for label in sorted(set(old_a) | set(new_a)):
        if old_a.get(label) != new_a.get(label):
            changes.append({"type": "atom", "id": label})
    old_b = [(b.a, b.b, b.order) for b in old.bonds]
    new_b = [(b.a, b.b, b.order) for b in new.bonds]
    if sorted(old_b) != sorted(new_b):
        changes.append({"type": "bond", "id": "bonds"})
    old_pr = {p.label: p.to_dict() for p in old.priors}
    new_pr = {p.label: p.to_dict() for p in new.priors}
    for label in sorted(set(old_pr) | set(new_pr)):
        if old_pr.get(label) != new_pr.get(label):
            changes.append({"type": "prior", "id": label})
    if old.params != new.params:
        for name in sorted(set(old.params) | set(new.params)):
            if old.params.get(name) != new.params.get(name):
                changes.append({"type": "param", "id": name,
                                "before": old.params.get(name),
                                "after": new.params.get(name)})
    return changes
