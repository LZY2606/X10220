"""Read-model builder: staleness, conflicts and propagation impact.

Derived views never overwrite history. A run stays inspectable forever; it is
annotated ``current`` / ``stale`` / ``expired`` against the branch head.
"""
from __future__ import annotations

from .chemistry import affected_sites
from .events import initial_state, reduce_event, visible_event_ids

CONDITION_KEYS = ("solvent", "temperature", "reference")


def branch_state(state: dict, branch: str) -> dict:
    ids = set(visible_event_ids(state, branch))
    sub = initial_state()
    sub["project_id"] = state["project_id"]
    for event in state["events"]:
        if event["id"] in ids:
            sub = reduce_event(sub, event)
    return sub


def _condition_changed(run_record: dict, head_sources: dict) -> dict:
    """Return {nucleus: changed condition keys} for solvents/temp/reference."""
    changed = {}
    snapshot = run_record.get("conditions", {})
    for nuc in ("1H", "13C"):
        current = head_sources.get(nuc)
        if current is None or nuc not in snapshot:
            continue
        diffs = []
        for key in CONDITION_KEYS:
            old_val = snapshot[nuc].get(key)
            new_val = current["parsed"]["conditions"].get(key)
            if _norm(old_val) != _norm(new_val):
                diffs.append(key)
        if diffs:
            changed[nuc] = diffs
    return changed


def _norm(value):
    if isinstance(value, dict):
        return value.get("raw")
    return value


def _run_map(runs: list[dict]) -> dict[str, dict]:
    return {run["run_id"]: run for run in runs}


def build_view(state: dict, branch: str = "main") -> dict:
    sub = branch_state(state, branch)
    head_sources = sub["sources"]
    events_on_branch = [e for e in state["events"] if e["id"] in
                        set(visible_event_ids(state, branch))]

    # Latest local-change event per nucleus (used for content-based staleness).
    local_events = {}
    for event in events_on_branch:
        if event["kind"] in ("source_submitted", "source_revised"):
            role = event["payload"]["role"]
            local_events[role] = event

    annotated_runs = []
    previous_by_basis: dict[tuple, dict] = {}
    for run in sub["runs"]:
        annotations = _annotate_run(run, head_sources, sub, local_events)
        annotated_runs.append(annotations)

    latest_by_site = _latest_current_by_site(annotated_runs)
    decisions_view = _annotate_decisions(sub, annotated_runs)
    conflicts = detect_conflicts(decisions_view)

    return {
        "project_id": state["project_id"],
        "title": state["title"],
        "branch": branch,
        "branches": sorted(state["branches"]),
        "head_event_id": state["branch_heads"].get(branch),
        "sources": _sources_view(sub),
        "params": sub["params"],
        "runs": annotated_runs,
        "latest_by_site": latest_by_site,
        "decisions": decisions_view,
        "conflicts": conflicts,
        "events": events_on_branch,
        "skeleton": head_sources.get("skeleton", {}).get("parsed"),
    }


def _annotate_run(run: dict, head_sources: dict, sub: dict,
                  local_events: dict) -> dict:
    """Annotate a run with current/stale/expired and per-site freshness."""
    annotated = dict(run)
    basis = run["basis_versions"]
    site_freshness = {}
    reasons = {}

    condition_changed = _condition_changed(run, head_sources)

    # Latest run for the same skeleton+params basis drives content staleness.
    newer_same_basis = [
        r for r in sub["runs"]
        if r["run_index"] > run["run_index"]
        and r["params_fingerprint"] == run["params_fingerprint"]
        and r["basis_versions"].get("skeleton") == basis.get("skeleton")
        and all(r["basis_versions"].get(nuc) == head_sources.get(nuc, {}).get("version")
                for nuc in ("1H", "13C") if nuc in head_sources)
    ]
    newest_same = newer_same_basis[-1] if newer_same_basis else None

    diff_sites = set()
    if newest_same is not None:
        diff_sites = set(affected_sites(run, newest_same))

    for result in run["site_results"]:
        site_id = result["site_id"]
        nucleus = "1H" if result["nucleus"] == "H" else "13C"
        site_reasons = []
        fresh = True

        if nucleus in condition_changed:
            fresh = False
            site_reasons.append(
                "conditions-changed:" + ",".join(condition_changed[nucleus]))

        head_version = head_sources.get(nucleus, {}).get("version")
        if head_version is not None and basis.get(nucleus) != head_version and site_id in diff_sites:
            fresh = False
            site_reasons.append(
                f"source-revised:{nucleus}:{basis.get(nucleus)}->{head_version}")

        site_freshness[site_id] = fresh
        reasons[site_id] = site_reasons

    annotated["site_freshness"] = site_freshness
    annotated["staleness_reasons"] = reasons
    if all(not fresh for fresh in site_freshness.values()) and site_freshness:
        annotated["state"] = "expired"
    elif any(not fresh for fresh in site_freshness.values()):
        annotated["state"] = "stale-partial"
    elif sub["params"] is not None and \
            run["params_fingerprint"] != _params_fp(sub["params"]):
        annotated["state"] = "stale-params"
    else:
        annotated["state"] = "current"
    return annotated


def _params_fp(params: dict) -> str:
    from .ident import fingerprint
    return fingerprint(params, length=12)


def _latest_current_by_site(runs: list[dict]) -> dict[str, dict]:
    latest = {}
    for run in sorted(runs, key=lambda r: r["run_index"]):
        for result in run["site_results"]:
            if run["site_freshness"].get(result["site_id"], False):
                latest[result["site_id"]] = {
                    "run_id": run["run_id"],
                    "run_index": run["run_index"],
                    "run_state": run["state"],
                    "result": result,
                }
    return latest


def _annotate_decisions(sub, runs) -> list[dict]:
    run_by_id = {r["run_id"]: r for r in runs}
    resolved_sites: dict[str, dict] = {}
    for resolution in sub.get("resolutions", []):
        resolved_sites[resolution["site_id"]] = resolution
    decisions = []
    for decision in sub["decisions"]:
        run = run_by_id.get(decision["run_id"])
        status = "active"
        detail = ""
        if run is None:
            status = "not-on-branch"
        else:
            fresh = run["site_freshness"].get(decision["site_id"], False)
            if not fresh:
                status = "expired-replayable"
                detail = "; ".join(run["staleness_reasons"].get(decision["site_id"], []))
        resolution = resolved_sites.get(decision["site_id"])
        if resolution is not None and resolution["resolution"] == "choose":
            if decision["decision_id"] != resolution.get("chosen_decision_id"):
                status = "superseded-by-resolution"
                detail = f"conflict resolved at {resolution['event_id']}"
            else:
                status = "active-resolved"
        elif resolution is not None and resolution["resolution"] == "keep_all":
            status = "active-kept-all"
        decisions.append({**decision, "status": status, "staleness_detail": detail})
    return decisions


def detect_conflicts(decisions: list[dict]) -> list[dict]:
    """Two editors accept different candidates for the same current site."""
    accepts = [d for d in decisions if d["action"] == "accept"]
    by_site: dict[str, list[dict]] = {}
    for decision in accepts:
        by_site.setdefault(decision["site_id"], []).append(decision)
    conflicts = []
    for site_id, site_decisions in by_site.items():
        unresolved = [d for d in site_decisions
                      if d["status"] not in ("superseded", "superseded-by-resolution")]
        editors = {d["editor"] for d in unresolved}
        candidates = {d["candidate_key"] for d in unresolved}
        if len(editors) >= 2 and len(candidates) >= 2:
            conflicts.append({
                "site_id": site_id,
                "decision_ids": sorted(d["decision_id"] for d in unresolved),
                "editors": sorted(editors),
                "candidate_keys": sorted(c for c in candidates if c),
            })
    return conflicts


def _sources_view(sub: dict) -> dict:
    view = {}
    for role, chain in sub["source_chains"].items():
        current = chain[-1]
        view[role] = {
            "role": role,
            "version": current["version"],
            "parent_version": current["parent_version"],
            "filename": current["filename"],
            "content_sha": current["content_sha"],
            "parser_version": current["parser_version"],
            "history": [{"version": v["version"], "parent_version": v["parent_version"],
                         "content_sha": v["content_sha"], "filename": v["filename"]}
                        for v in chain],
            "warnings": current["parsed"].get("warnings", []),
        }
    return view


def propagation_impact(state: dict, role: str, new_version_parsed: dict,
                       branch: str = "main") -> dict:
    """Preview which sites a new source version would invalidate."""
    from .chemistry import affected_sites, run_candidates
    sub = branch_state(state, branch)
    skeleton = sub["sources"]["skeleton"]["parsed"]
    sources = {nuc: sub["sources"][nuc]["parsed"] for nuc in ("1H", "13C")}
    before_runs = [r for r in sub["runs"]]
    previous = before_runs[-1] if before_runs else None
    trial_sources = dict(sources)
    if role in trial_sources:
        trial_sources[role] = new_version_parsed
    params = sub["params"]
    from .chemistry import DEFAULT_PARAMS
    trial = run_candidates(skeleton, trial_sources, params or DEFAULT_PARAMS,
                           run_index=(previous["run_index"] + 1) if previous else 1,
                           basis_versions={r: "PREVIEW" for r in ("skeleton", "1H", "13C")})
    sites = affected_sites(previous, trial) if previous else []
    return {"role": role, "affected_sites": sites,
            "unchanged_sites": [r["site_id"] for r in trial["site_results"]
                                if r["site_id"] not in sites]}
