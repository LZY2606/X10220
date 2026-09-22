"""Event-sourced state reconstruction.

The event table is the single source of truth. State at any branch and, optionally,
at any past event seq is reconstructed by replay; derived caches never overwrite
history (POL-009).
"""
from .constants import (
    POLICY_LOCAL_DEPENDENCY_STALE,
    POLICY_REFERENCE_BIAS_STALE,
    DECISION_ACCEPT,
    DECISION_REJECT,
    DECISION_RETRACT,
    STATUS_CURRENT,
    STATUS_EXPIRED,
    STATUS_SUPERSEDED,
    STATUS_UNKNOWN,
)


def branch_ancestry(branches_by_uid, branch_uid):
    chain = []
    cur = branch_uid
    guard = 0
    while cur and guard < 10000:
        chain.append(cur)
        b = branches_by_uid.get(cur)
        if not b:
            break
        cur = b["parent_branch_uid"]
        guard += 1
    return chain


def _source_fingerprint(src):
    return "%s@v%s" % (src["uid"], src["version"])


def compute_invalidation(current_run, new_versions):
    """Return (stale candidate uids, affected nuclei) for one direct source change.

    Only candidates whose direct dependency changed are stale (POL-008 / POL-004).
    """
    stale = set()
    nuclei = set()
    sv = current_run.get("source_versions", {})
    for cand in current_run["candidates"]:
        dep = cand.get("dependencies", {})
        hit = False
        mol = dep.get("molecule_uid")
        if "molecule" in new_versions and sv.get("molecule", {}).get("uid") == mol:
            hit = True
        peak_src = dep.get("peak_source_uid")
        for key in ("peaks_1h", "peaks_13c"):
            if key in new_versions and sv.get(key, {}).get("uid") == peak_src:
                hit = True
                nuclei.add(cand["nucleus"])
        if set(new_versions) & {"cosy", "hsqc", "hmbc"}:
            edge_uids = set(sv.get(k, {}).get("uid") for k in ("cosy", "hsqc", "hmbc"))
            if set(dep.get("edge_source_uids", [])) & edge_uids:
                hit = True
        if hit:
            stale.add(cand["uid"])
            nuclei.add(cand["nucleus"])
    return stale, nuclei


def _event_policy(kind):
    return {
        "source_imported": "POL-007",
        "param_revision": "POL-008",
        "run_generated": "POL-005",
        "review_decision": "POL-006",
        "conflict_detected": "POL-006",
        "conflict_resolved": "POL-006",
        "decision_retracted": "POL-009",
    }.get(kind)


def build_state(store, project_uid, branch_uid=None, at_seq=None):
    project = store.get_project(project_uid)
    if not project:
        return None
    branches = store.list_branches(project_uid)
    branches_by_uid = {b["uid"]: b for b in branches}
    if branch_uid is None:
        branch_uid = branches[0]["uid"] if branches else None
    chain = set(branch_ancestry(branches_by_uid, branch_uid))

    events = [e for e in store.list_project_events(project_uid)
              if e["branch_uid"] in chain and (at_seq is None or e["seq"] <= at_seq)]
    events.sort(key=lambda e: e["seq"])

    sources = {}
    versions_by_kind = {}
    params = None
    param_history = []
    runs = []
    decisions = []
    invalidations = []
    conflict_events = []
    resolved_conflict_keys = set()
    retracted_decision_uids = set()

    for event in events:
        p = event["payload"]
        kind = event["kind"]
        if kind == "source_imported":
            src = p["source"]
            current_run = runs[-1] if runs and runs[-1]["status"] == STATUS_CURRENT else None
            prev = sources.get(src["kind"])
            sources[src["kind"]] = src
            versions_by_kind.setdefault(src["kind"], []).append(
                {"uid": src["uid"], "version": src["version"]})
            invalidation = None
            if prev and current_run:
                stale_uids, nuclei = compute_invalidation(
                    current_run, {src["kind"]: src["uid"]})
                invalidation = {"changed_kind": src["kind"],
                                "new_source_uid": src["uid"],
                                "new_version": src["version"],
                                "stale_candidate_uids": sorted(stale_uids),
                                "affected_nuclei": sorted(nuclei),
                                "policy": (POLICY_REFERENCE_BIAS_STALE
                                           if src["kind"] in ("peaks_1h", "peaks_13c")
                                           else POLICY_LOCAL_DEPENDENCY_STALE)}
                for cand in current_run["candidates"]:
                    if cand["uid"] in stale_uids:
                        cand["status"] = STATUS_EXPIRED
                current_run["status"] = STATUS_EXPIRED if stale_uids else STATUS_CURRENT
                invalidations.append(invalidation)
            p_out = dict(p)
            p_out["invalidation"] = invalidation
            event = dict(event)
            event["payload"] = p_out
        elif kind == "param_revision":
            params = p["params"]
            param_history.append({"params": params, "fingerprint": p["fingerprint"],
                                  "seq": event["seq"], "actor": event["actor"]})
            if runs and runs[-1]["status"] == STATUS_CURRENT:
                runs[-1]["status"] = STATUS_EXPIRED
                for cand in runs[-1]["candidates"]:
                    cand["status"] = STATUS_EXPIRED
        elif kind == "run_generated":
            run = p["run"]
            run["status"] = STATUS_CURRENT
            for cand in run["candidates"]:
                cand["status"] = STATUS_CURRENT
                if not cand.get("decision"):
                    cand["decision"] = None
            runs.append(run)
            if len(runs) >= 2:
                for older in runs[:-1]:
                    if older["status"] == STATUS_CURRENT:
                        older["status"] = STATUS_SUPERSEDED
                        for cand in older["candidates"]:
                            if cand["status"] == STATUS_CURRENT:
                                cand["status"] = STATUS_SUPERSEDED
        elif kind == "review_decision":
            dec = p["decision"]
            decisions.append(dec)
            run = next((r for r in reversed(runs) if r["run_uid"] == dec["run_uid"]), None)
            if run:
                cand = next((c for c in run["candidates"] if c["uid"] == dec["candidate_uid"]), None)
                if cand:
                    cand["decision"] = {
                        "decision_uid": dec["uid"], "verdict": dec["verdict"],
                        "actor": dec["actor"], "seq": event["seq"], "note": dec.get("note", "")}
        elif kind == "decision_retracted":
            retracted_decision_uids.add(p["decision_uid"])
            for run in runs:
                for cand in run["candidates"]:
                    if cand.get("decision") and cand["decision"]["decision_uid"] == p["decision_uid"]:
                        cand["decision"] = None
        elif kind == "conflict_resolved":
            resolved_conflict_keys.add(p["conflict_key"])
        elif kind == "conflict_detected":
            conflict_events.append(event)

    # annotate stale decisions (decision recorded against a candidate version no longer current)
    latest_run = next((r for r in reversed(runs) if r["status"] in (STATUS_CURRENT, STATUS_EXPIRED)), None)
    current_run_live = runs[-1] if runs and runs[-1]["status"] == STATUS_CURRENT else None

    open_conflicts = []
    for event in conflict_events:
        for conflict in event["payload"].get("conflicts", []):
            if conflict["key"] in resolved_conflict_keys:
                continue
            if any(d in retracted_decision_uids for d in conflict["decision_uids"]):
                continue
            open_conflicts.append(dict(conflict, detected_seq=event["seq"]))

    # decisions that reference expired/superseded runs remain replayable but flagged stale
    decision_view = []
    for dec in decisions:
        d = dict(dec)
        d["retracted"] = dec["uid"] in retracted_decision_uids
        run = next((r for r in runs if r["run_uid"] == dec["run_uid"]), None)
        d["run_status"] = run["status"] if run else STATUS_UNKNOWN
        decision_view.append(d)

    tied_summary = []
    if current_run_live:
        by_atom = {}
        for c in current_run_live["candidates"]:
            by_atom.setdefault(c["atom_id"], []).append(c)
        for atom_id, cands in by_atom.items():
            if sum(1 for c in cands if c["is_tied_best"]) > 1:
                tied_summary.append({"atom_id": atom_id,
                                     "candidate_uids": [c["uid"] for c in cands if c["is_tied_best"]]})

    return {
        "project": project,
        "current_branch_uid": branch_uid,
        "branches": branches,
        "events": events,
        "sources": sources,
        "versions_by_kind": versions_by_kind,
        "params": params,
        "param_history": param_history,
        "runs": runs,
        "current_run": current_run_live,
        "decisions": decision_view,
        "invalidations": invalidations,
        "open_conflicts": open_conflicts,
        "resolved_conflict_keys": sorted(resolved_conflict_keys),
        "tied_atoms": tied_summary,
        "at_seq": at_seq,
    }
