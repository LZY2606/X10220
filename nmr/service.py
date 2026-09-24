from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from typing import Any

from .clock import iso_now
from .engine import DEFAULT_PARAMS, generate_candidates, parameter_fingerprint
from .model import build_model
from .parser import parse_source
from .storage import Store


class ProjectError(ValueError):
    pass


def create_project(store: Store, name: str, project_id: str | None = None, created_at: str | None = None) -> str:
    return store.create_project(name, project_id, created_at)


def commit_source(
    store: Store,
    project_id: str,
    filename: str,
    content: str,
    editor_id: str = "reviewer",
    created_at: str | None = None,
    parent_event_ids: list[str] | None = None,
    branch_name: str = "main",
) -> dict[str, Any]:
    parsed = parse_source(filename, content).as_dict()
    text_hash = _sha256(content)
    doc_id = f"src:{text_hash[:10]}:{filename}"
    payload = {
        "filename": filename,
        "content": content,
        "doc_id": doc_id,
        "parser_version": parsed["parser_version"],
        "parsed_records": parsed["records"],
        "diagnostics": parsed["diagnostics"],
        "immutability": "source_content_is_immutable; revisions express a new version and parent_doc_id",
        "parent_doc_id": None,
    }
    event_id = store.append_event(project_id, "SOURCE_COMMITTED", payload, parent_event_ids, branch_name, editor_id, created_at)
    return {"event_id": event_id, "doc_id": doc_id, "parsed": parsed}


def commit_source_revision(
    store: Store,
    project_id: str,
    parent_doc_id: str,
    filename: str,
    content: str,
    editor_id: str = "reviewer",
    created_at: str | None = None,
    parent_event_ids: list[str] | None = None,
    branch_name: str = "main",
) -> dict[str, Any]:
    parsed = parse_source(filename, content).as_dict()
    doc_id = f"src:{_sha256(content)[:10]}:{filename}"
    payload = {
        "filename": filename,
        "content": content,
        "doc_id": doc_id,
        "parent_doc_id": parent_doc_id,
        "parser_version": parsed["parser_version"],
        "parsed_records": parsed["records"],
        "diagnostics": parsed["diagnostics"],
        "immutability": "new_source_version;_parent_is_retained",
    }
    event_id = store.append_event(project_id, "SOURCE_REVISED", payload, parent_event_ids, branch_name, editor_id, created_at)
    return {"event_id": event_id, "doc_id": doc_id, "parsed": parsed}


def revise_parameters(
    store: Store,
    project_id: str,
    params: dict[str, Any],
    reason: str,
    editor_id: str = "reviewer",
    created_at: str | None = None,
    parent_event_ids: list[str] | None = None,
    branch_name: str = "main",
) -> str:
    payload = {"params": params, "parameter_fingerprint": parameter_fingerprint(params), "reason": reason, "scope": "parameter_revision"}
    return store.append_event(project_id, "PARAMETERS_REVISED", payload, parent_event_ids, branch_name, editor_id, created_at)


def run_candidates(
    store: Store,
    project_id: str,
    editor_id: str = "reviewer",
    created_at: str | None = None,
    parent_event_ids: list[str] | None = None,
    branch_name: str = "main",
) -> str:
    state = project_at(store, project_id, parent_event_ids, branch_name=branch_name)
    serializable = _model_to_json(state["model"])
    result = generate_candidates(state["model"], state["parameters"])
    condition_fingerprint = _condition_fingerprint(state["active_sources"])
    run_id = "run:" + _sha256(json.dumps({"model": serializable, "params": state["parameters"], "algo": result["algorithm_version"]}, sort_keys=True, ensure_ascii=False))[:12]
    payload = {
        "run_id": run_id,
        "algorithm_version": result["algorithm_version"],
        "parameter_fingerprint": result["parameter_fingerprint"],
        "condition_fingerprint": condition_fingerprint,
        "active_source_doc_ids": [source["doc_id"] for source in state["active_sources"]],
        "candidates": result["candidates"],
        "peak_summaries": result["peak_summaries"],
        "global_bias": result["global_bias"],
        "unknowns": result["unknowns"],
        "diagnostics": state["model"]["diagnostics"],
        "derived_policy": "derived values never overwrite sources; stale artifacts remain readable",
    }
    return store.append_event(project_id, "CANDIDATES_RUN", payload, parent_event_ids, branch_name, editor_id, created_at)


def record_decision(
    store: Store,
    project_id: str,
    candidate_id: str,
    action: str,
    rationale: str = "",
    editor_id: str = "reviewer",
    created_at: str | None = None,
    parent_event_ids: list[str] | None = None,
    source_branch_name: str | None = None,
) -> tuple[str, str | None, str | None]:
    if action not in {"accepted", "rejected"}:
        raise ProjectError("decision action must be accepted or rejected")
    state = project_at(store, project_id, parent_event_ids, branch_name=source_branch_name)
    candidate = _find_current_candidate(state, candidate_id)
    conflict_with = _conflicting_decision(store, project_id, candidate["stable_key"], action, parent_event_ids)
    base_parents = parent_event_ids if parent_event_ids is not None else store.head_ids(project_id, source_branch_name or "main")
    branch_name = f"review/{editor_id}/{_short(candidate_id)}"
    payload = {
        "candidate_id": candidate_id,
        "candidate_stable_key": candidate["stable_key"],
        "run_id": candidate["run_id"],
        "peak_id": candidate["peak_id"],
        "atom_id": candidate["atom_id"],
        "action": action,
        "rationale": rationale,
        "propagation": _propagation(candidate, state),
        "machine_judgment": {
            "overlap_and_missing_peaks": "never_force_unique_match",
            "coupling_network": "evidence_only",
            "condition_change": "expire_dependent_candidates_but_preserve_replayable_decisions",
            "ties": "retain_all_legal_rank_one_candidates",
        },
    }
    decision_event_id = store.append_event(project_id, "DECISION_RECORDED", payload, base_parents, branch_name, editor_id, created_at)
    conflict_event_id = None
    if conflict_with:
        conflict_payload = {
            "conflict_id": "conflict:" + _sha256(candidate["stable_key"])[:10],
            "candidate_stable_key": candidate["stable_key"],
            "winner_decision_event_id": decision_event_id,
            "previous_decision_event_id": conflict_with["event_id"],
            "status": "detected",
            "resolution": "branch_created; neither decision overwrites the other",
            "affected_object_ids": [candidate_id, candidate["peak_id"], candidate["atom_id"]],
        }
        conflict_event_id = store.append_event(project_id, "CONFLICT_DETECTED", conflict_payload, [decision_event_id, conflict_with["event_id"]], branch_name, editor_id, created_at)
    return decision_event_id, conflict_event_id, branch_name


def resolve_conflict(
    store: Store,
    project_id: str,
    conflict_id: str,
    resolution: str,
    chosen_event_id: str,
    editor_id: str = "lead-reviewer",
    created_at: str | None = None,
    parent_event_ids: list[str] | None = None,
    branch_name: str = "main",
) -> str:
    if resolution not in {"keep_chosen", "reopen", "supersede_other"}:
        raise ProjectError("unsupported conflict resolution")
    payload = {"conflict_id": conflict_id, "resolution": resolution, "chosen_event_id": chosen_event_id, "historical_decisions_retained": True}
    return store.append_event(project_id, "CONFLICT_RESOLVED", payload, parent_event_ids, branch_name, editor_id, created_at)


def project_at(store: Store, project_id: str, head_event_ids: list[str] | None = None, branch_name: str | None = None) -> dict[str, Any]:
    events = store.list_events(project_id)
    path = _ancestor_path(events, head_event_ids, branch_name)
    source_events = [event for event in path if event["event_type"] in {"SOURCE_COMMITTED", "SOURCE_REVISED"}]
    active_sources = _active_sources(source_events)
    model = build_model(active_sources)
    parameters = copy.deepcopy(DEFAULT_PARAMS)
    parameter_event = None
    run = None
    decisions: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for event in path:
        if event["event_type"] == "PARAMETERS_REVISED":
            parameters = copy.deepcopy(event["payload"]["params"])
            parameter_event = event
        elif event["event_type"] == "CANDIDATES_RUN":
            run = _decorate_run(event, active_sources, parameters)
        elif event["event_type"] == "DECISION_RECORDED":
            decisions[event["payload"]["candidate_stable_key"]] = event
        elif event["event_type"] in {"CONFLICT_DETECTED", "CONFLICT_RESOLVED"}:
            conflicts.append(event)
    return {
        "project_id": project_id,
        "view_event_ids": [event["event_id"] for event in path],
        "events": [_public_event(event) for event in path],
        "active_sources": active_sources,
        "model": model,
        "parameters": parameters,
        "parameter_event_id": parameter_event["event_id"] if parameter_event else None,
        "run": run,
        "decisions": [_public_event(event) for event in decisions.values()],
        "conflicts": [_public_event(event) for event in conflicts],
        "global_cross_branch_conflicts": [_public_event(event) for event in _all_conflicts(events)],
        "current_source_doc_ids": sorted({source["doc_id"] for source in _active_sources([event for event in events if event["event_type"] in {"SOURCE_COMMITTED", "SOURCE_REVISED"}])}),
    }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _short(value: str) -> str:
    return _sha256(value)[:8]


def _model_to_json(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "atoms": [asdict(atom) for atom in model["molecule"]["atoms"].values()] if model.get("molecule") else [],
        "bonds": list(model["molecule"]["bonds"]) if model.get("molecule") else [],
        "peaks": [asdict(peak) for peak in model["peaks"]],
        "edges": [asdict(edge) for edge in model["edges"]],
        "diagnostics": model["diagnostics"],
    }


def _active_sources(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_filename: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event["payload"]
        parsed = {
            "kind": _kind_for_filename(payload["filename"]),
            "records": payload.get("records", _records_from_event(payload)),
            "diagnostics": payload.get("diagnostics", []),
            "parser_version": payload.get("parser_version", "unknown"),
        }
        item = {"doc_id": payload["doc_id"], "filename": payload["filename"], "content": payload["content"], "event_id": event["event_id"], "parsed": parsed}
        by_filename[payload["filename"]] = item
    return [by_filename[name] for name in sorted(by_filename)]


def _kind_for_filename(filename: str) -> str:
    suffix = filename.rsplit(".", 1)[-1].lower()
    return {"mol": "molecule", "1d": "peak1d", "2d": "peak2d"}[suffix]


def _records_from_event(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return payload.get("parsed_records", {"meta": [], "atoms": [], "bonds": [], "peaks": [], "edges": [], "raw_records": []})


def _ancestor_path(all_events: list[dict[str, Any]], head_event_ids: list[str] | None, branch_name: str | None) -> list[dict[str, Any]]:
    by_id = {event["event_id"]: event for event in all_events}
    if head_event_ids is None:
        heads = _branch_heads(all_events, branch_name or "main")
    else:
        heads = list(head_event_ids)
    selected: set[str] = set()
    stack = list(heads)
    while stack:
        event_id = stack.pop()
        if event_id in selected or event_id not in by_id:
            continue
        selected.add(event_id)
        stack.extend(by_id[event_id]["parent_event_ids"])
    return [event for event in all_events if event["event_id"] in selected]


def _branch_heads(events: list[dict[str, Any]], branch_name: str) -> list[str]:
    branch_events = [event for event in events if event["branch_name"] == branch_name]
    if not branch_events:
        branch_events = events
    if not branch_events:
        return []
    ids = {event["event_id"] for event in branch_events}
    referenced: set[str] = set()
    for event in branch_events:
        referenced.update(parent for parent in event["parent_event_ids"] if parent in ids)
    heads = ids - referenced
    if heads:
        return [event["event_id"] for event in branch_events if event["event_id"] in heads]
    return [branch_events[-1]["event_id"]]


def _decorate_run(run_event: dict[str, Any], active_sources: list[dict[str, Any]], current_params: dict[str, Any]) -> dict[str, Any]:
    current_conditions = _condition_fingerprint(active_sources)
    current_source_ids = {source["doc_id"] for source in active_sources}
    run = dict(run_event["payload"])
    run["event_id"] = run_event["event_id"]
    run["current"] = run.get("condition_fingerprint") == current_conditions and run.get("parameter_fingerprint") == parameter_fingerprint(current_params) and set(run.get("active_source_doc_ids", [])) == current_source_ids
    run["stale"] = not run["current"]
    stale_reasons = []
    if run.get("condition_fingerprint") != current_conditions:
        stale_reasons.append("solvent_temperature_reference_or_condition_source_changed")
    if run.get("parameter_fingerprint") != parameter_fingerprint(current_params):
        stale_reasons.append("parameter_fingerprint_changed")
    if set(run.get("active_source_doc_ids", [])) != current_source_ids:
        stale_reasons.append("active_source_versions_changed")
    run["stale_reasons"] = stale_reasons
    decisions_by_key = {}
    candidates = []
    for candidate in run.get("candidates", []):
        item = dict(candidate)
        item["run_id"] = run["run_id"]
        item["stale"] = run["stale"]
        item["current"] = run["current"]
        candidates.append(item)
    run["candidates"] = candidates
    return run


def _condition_fingerprint(sources: list[dict[str, Any]]) -> str:
    fragments = []
    for source in sources:
        for meta in source["parsed"]["records"]["meta"]:
            keep = {key: meta[key] for key in sorted(meta) if key in {"solvent", "temperature_k", "reference", "spectrometer_mhz", "nucleus", "experiment"}}
            fragments.append({"doc_id": source["doc_id"], "conditions": keep})
    return _sha256(json.dumps(fragments, sort_keys=True, ensure_ascii=False))[:16]


def _find_current_candidate(state: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    run = state.get("run")
    if run is None:
        raise ProjectError("no candidate run exists on the selected path")
    for candidate in run["candidates"]:
        if candidate["candidate_id"] == candidate_id:
            return candidate
    raise ProjectError(f"candidate {candidate_id} is not present in run {run['run_id']}")


def _conflicting_decision(store: Store, project_id: str, stable_key: str, action: str, parents: list[str] | None) -> dict[str, Any] | None:
    events = store.list_events(project_id)
    parent_set = set(parents or _branch_heads(events, "main"))
    ancestors = {event["event_id"] for event in _ancestor_path(events, list(parent_set), "main")}
    for event in reversed(events):
        if event["event_type"] != "DECISION_RECORDED" or event["event_id"] in ancestors:
            continue
        payload = event["payload"]
        if payload.get("candidate_stable_key") == stable_key and payload.get("action") != action:
            return event
    return None


def _propagation(candidate: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "affected_peak_id": candidate["peak_id"],
        "affected_atom_id": candidate["atom_id"],
        "dependent_candidate_stable_keys": [candidate["stable_key"]],
        "unchanged_peak_ids": [peak.peak_id for peak in state["model"]["peaks"] if peak.peak_id != candidate["peak_id"]],
        "history_policy": "decision creates a branch and does not mutate candidate calculations",
    }


def _all_conflicts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["event_type"] in {"CONFLICT_DETECTED", "CONFLICT_RESOLVED"}]


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    return dict(event)
