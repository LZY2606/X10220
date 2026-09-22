from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .parser import parse_source_document
from .scoring import calculate_candidates, default_parameters, parameter_fingerprint
from . import store


@dataclass
class ReplayState:
    project_id: str
    name: str = ""
    current_source_id: Optional[str] = None
    current_source_version: Optional[str] = None
    sources: Dict[Tuple[str, str], Dict[str, Any]] = None
    parameters: Dict[str, Any] = None
    parameter_rule_version: str = "rules-v1"
    decisions: Dict[str, Dict[str, Any]] = None
    decision_order: List[str] = None
    conflicts: Dict[str, Dict[str, Any]] = None
    branches: Dict[str, Dict[str, str]] = None
    last_state_event_id: Optional[str] = None
    last_state_event_type: Optional[str] = None
    current_result: Optional[Dict[str, Any]] = None
    stale_results: List[Dict[str, Any]] = None
    current_run_status: str = "current"
    invalidations: List[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = None

    def __post_init__(self):
        self.sources = self.sources or {}
        self.parameters = self.parameters or default_parameters()
        self.decisions = self.decisions or {}
        self.decision_order = self.decision_order or []
        self.conflicts = self.conflicts or {}
        self.branches = self.branches or {}
        self.stale_results = self.stale_results or []
        self.invalidations = self.invalidations or []
        self.events = self.events or []


def _event_to_dict(row) -> Dict[str, Any]:
    item = dict(row)
    item["payload"] = store.loads(item.pop("payload_json"))
    return item


def _source_digest_keys(parsed: Dict[str, Any]) -> Set[str]:
    keys = {f"source:{parsed['file_version']}"}
    for nucleus in ("1H", "13C"):
        for peak in parsed["documents"][nucleus]["peaks"]:
            keys.add(peak["stable_id"])
    for experiment in ("COSY", "HSQC", "HMBC"):
        for edge in parsed["documents"][experiment]["edges"]:
            keys.add(edge["stable_id"])
    for atom_id in parsed["molecule"]["atoms"]:
        keys.add(f"atom:{atom_id}")
    keys.update(set(__import__("nmr.parser", fromlist=["source_metadata_keys"]).source_metadata_keys(parsed)))
    return keys


def _changed_direct_sources(old_parsed: Optional[Dict[str, Any]], new_parsed: Dict[str, Any], changed_fields: Optional[List[str]]) -> Set[str]:
    new_keys = _source_digest_keys(new_parsed)
    if not old_parsed:
        return set(new_keys)
    old_keys = _source_digest_keys(old_parsed)
    changed = new_keys ^ old_keys
    if changed_fields:
        changed.update({f"field:{field}" for field in changed_fields})
        metadata_fields = {"metadata.solvent", "metadata.temperature_k", "metadata.reference_standard",
                           "one_dimensional.1H.metadata.solvent", "one_dimensional.1H.metadata.temperature_k",
                           "one_dimensional.1H.metadata.reference_standard",
                           "one_dimensional.13C.metadata.solvent", "one_dimensional.13C.metadata.temperature_k",
                           "one_dimensional.13C.metadata.reference_standard"}
        if changed_fields and any(field in metadata_fields for field in changed_fields):
            changed.update({key for key in new_keys if key.startswith("meta:")})
    return changed


def _mark_stale_candidates(result: Optional[Dict[str, Any]], changed: Set[str], reason: str, event_id: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if not result:
        return None, []
    stale_ids = []
    for candidate in result.get("candidates", []):
        if changed and set(candidate.get("dependencies", [])) & changed:
            stale_ids.append(candidate["stable_id"])
    for unknown in result.get("unknowns", []):
        if changed and set(unknown.get("dependencies", [])) & changed:
            stale_ids.append(unknown["stable_id"])
    stale_result = {
        **result,
        "status": "stale",
        "stale_reason": reason,
        "invalidated_by_event": event_id,
        "stale_candidate_ids": sorted(set(stale_ids)),
    }
    invalidations = [{"stable_id": stable_id, "reason": reason, "by_event": event_id} for stable_id in sorted(set(stale_ids))]
    return stale_result, invalidations


def _parse_stored(raw_text: str) -> Dict[str, Any]:
    return parse_source_document(raw_text)


def _result_signature(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    items = {}
    for key in ("candidates", "unknowns"):
        for item in result.get(key, []):
            items[item["stable_id"]] = {
                "score": item.get("score"),
                "state": item.get("state"),
                "dependencies": item.get("dependencies", []),
                "evidence_edge_ids": item.get("evidence_edge_ids", []),
                "parameter_fingerprint": item.get("parameter_fingerprint"),
            }
    return items


def _retire_changed_result(previous: Optional[Dict[str, Any]], current: Optional[Dict[str, Any]], reason: str, event_id: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if not previous:
        return None, []
    if current is None:
        changed_ids = sorted(_result_signature(previous).keys())
    else:
        old = _result_signature(previous)
        new = _result_signature(current)
        changed_ids = sorted(set(old) - set(new))
        for stable_id, old_value in old.items():
            if stable_id in new and old_value != new[stable_id]:
                changed_ids.append(stable_id)
        changed_ids = sorted(set(changed_ids))
    stale = {
        **previous,
        "status": "stale",
        "stale_reason": reason,
        "invalidated_by_event": event_id,
        "stale_candidate_ids": changed_ids,
    }
    invalidations = [{"stable_id": stable_id, "reason": reason, "by_event": event_id} for stable_id in changed_ids]
    return stale, invalidations


def replay(conn, project_id: str, branch: str = "main", at_event_id: Optional[str] = None) -> ReplayState:
    rows = store.branch_events(conn, project_id, branch, at_event_id)
    project_row = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    state = ReplayState(project_id=project_id, name=project_row["name"] if project_row else project_id)
    old_parsed: Optional[Dict[str, Any]] = None
    active_result: Optional[Dict[str, Any]] = None
    active_status = "current"
    for row in rows:
        event = _event_to_dict(row)
        state.events.append(event)
        payload = event["payload"]
        etype = event["type"]
        if etype == "project_created":
            state.name = payload.get("name", state.name)
        elif etype in {"source_imported", "source_revised"}:
            source_id = payload["source_id"]
            version = payload["file_version"]
            source_row = store.get_source_file(conn, project_id, source_id, version)
            if source_row:
                parsed = _parse_stored(source_row["raw_text"])
                state.sources[(source_id, version)] = {
                    "source_id": source_id,
                    "file_version": version,
                    "parent_version": payload.get("parent_version"),
                    "filename": source_row["filename"],
                    "sha256": source_row["content_sha256"],
                    "parsed": parsed,
                    "event_id": event["event_id"],
                }
                next_result = calculate_candidates(parsed, state.parameters)
                stale, invalidations = _retire_changed_result(active_result, next_result, etype, event["event_id"])
                if stale:
                    state.stale_results.append(stale)
                    state.invalidations.extend(invalidations)
                active_result = next_result
                active_status = "current"
                old_parsed = parsed
                state.current_source_id = source_id
                state.current_source_version = version
                state.last_state_event_id = event["event_id"]
                state.last_state_event_type = etype
        elif etype == "parameters_published":
            new_params = {**default_parameters(), **payload.get("parameters", {})}
            old_params = state.parameters
            if parameter_fingerprint(old_params) != parameter_fingerprint(new_params) and state.current_source_version:
                source = state.sources[(state.current_source_id, state.current_source_version)]
                next_result = calculate_candidates(source["parsed"], new_params)
                stale, invalidations = _retire_changed_result(active_result, next_result, "parameters_published", event["event_id"])
                if stale:
                    state.stale_results.append(stale)
                    state.invalidations.extend(invalidations)
                active_result = next_result
                active_status = "current"
            state.parameters = new_params
            state.parameter_rule_version = new_params.get("rule_version", payload.get("rule_version", "rules-v1"))
            state.last_state_event_id = event["event_id"]
            state.last_state_event_type = etype
        elif etype == "candidate_run_recorded":
            active_result = payload.get("result")
            active_status = payload.get("status", "current")
            state.last_state_event_id = event["event_id"]
            state.last_state_event_type = etype
        elif etype == "decision_made":
            decision = {
                "decision_id": payload["decision_id"],
                "candidate_id": payload.get("candidate_id"),
                "unknown_id": payload.get("unknown_id"),
                "atom_id": payload["atom_id"],
                "action": payload["action"],
                "actor": event["actor"],
                "event_id": event["event_id"],
                "rationale": payload.get("rationale", ""),
                "status": "active",
                "supersedes_conflict_id": payload.get("supersedes_conflict_id"),
            }
            old = state.decisions.get(decision["atom_id"])
            if old:
                old["status"] = "superseded"
            state.decisions[decision["atom_id"]] = decision
            state.decision_order.append(decision["decision_id"])
            state.last_state_event_id = event["event_id"]
            state.last_state_event_type = etype
        elif etype == "decision_revoked":
            atom_id = payload["atom_id"]
            if atom_id in state.decisions:
                state.decisions[atom_id]["status"] = "revoked"
            state.last_state_event_id = event["event_id"]
            state.last_state_event_type = etype
        elif etype == "conflict_recorded":
            state.conflicts[payload["conflict_id"]] = {**payload, "status": "open", "event_id": event["event_id"]}
        elif etype == "conflict_resolved":
            conflict_id = payload["conflict_id"]
            if conflict_id in state.conflicts:
                state.conflicts[conflict_id]["status"] = "resolved"
                state.conflicts[conflict_id]["resolution_event_id"] = event["event_id"]
                state.conflicts[conflict_id]["resolution"] = payload.get("resolution")
            state.last_state_event_id = event["event_id"]
            state.last_state_event_type = etype
        elif etype == "branch_created":
            state.branches[payload["branch"]] = {"parent_branch": payload.get("parent_branch"), "from_event": event["event_id"]}
    state.current_result = active_result
    state.current_run_status = active_status
    return state


def record_current_calculation(conn, project_id: str, branch: str, state: ReplayState, actor: str = "system") -> str:
    if not state.current_source_version:
        raise ValueError("import a source before calculating candidates")
    source = state.sources[(state.current_source_id, state.current_source_version)]
    result = calculate_candidates(source["parsed"], state.parameters)
    payload = {
        "source_id": state.current_source_id,
        "file_version": state.current_source_version,
        "algorithm_version": result["algorithm_version"],
        "parameter_fingerprint": result["parameter_fingerprint"],
        "run_id": result["run_id"],
        "result": result,
        "status": "current",
    }
    head = store.get_branch_head(conn, project_id, branch)
    event_id = store.append_event(conn, project_id=project_id, branch=branch, parent_event_id=head,
                                  event_type="candidate_run_recorded", actor=actor, payload=payload)
    conn.commit()
    return event_id


def decorate_graph(state: ReplayState) -> Dict[str, Any]:
    source = state.sources.get((state.current_source_id, state.current_source_version)) if state.current_source_version else None
    parsed = source["parsed"] if source else None
    result = state.current_result or {"candidates": [], "unknowns": [], "evidence": [], "run_id": None,
                                      "parameter_fingerprint": None, "algorithm_version": None, "parameters": state.parameters}
    candidates = []
    decision_by_candidate = {d.get("candidate_id"): d for d in state.decisions.values() if d.get("candidate_id")}
    for candidate in result.get("candidates", []):
        item = dict(candidate)
        decision = decision_by_candidate.get(candidate["stable_id"])
        atom_decision = state.decisions.get(candidate["atom_id"])
        item["manual_state"] = decision["status"] if decision and decision["action"] in ("accept", "reject") else None
        item["decision_id"] = decision["decision_id"] if decision else None
        item["blocked_by_active_decision"] = bool(
            atom_decision and atom_decision.get("candidate_id") and atom_decision.get("candidate_id") != candidate["stable_id"]
            and atom_decision["status"] == "active" and atom_decision["action"] == "accept"
        )
        candidates.append(item)
    unknowns = []
    for unknown in result.get("unknowns", []):
        item = dict(unknown)
        decision = state.decisions.get(unknown["atom_id"])
        item["decision_id"] = decision["decision_id"] if decision else None
        item["manual_state"] = decision["status"] if decision and decision.get("unknown_id") == unknown["stable_id"] else None
        unknowns.append(item)
    return {
        "project_id": state.project_id,
        "name": state.name,
        "branch": None,
        "source": None if not source else {
            "source_id": source["source_id"],
            "file_version": source["file_version"],
            "parent_version": source["parent_version"],
            "filename": source["filename"],
            "sha256": source["sha256"],
        },
        "parameters": result.get("parameters", state.parameters),
        "parameter_rule_version": state.parameter_rule_version,
        "molecule": parsed["molecule"] if parsed else None,
        "documents": parsed["documents"] if parsed else None,
        "diagnostics": parsed.get("diagnostics", []) if parsed else [],
        "candidates": candidates,
        "unknowns": unknowns,
        "evidence": result.get("evidence", []),
        "run_id": result.get("run_id"),
        "run_status": state.current_run_status,
        "algorithm_version": result.get("algorithm_version"),
        "parameter_fingerprint": result.get("parameter_fingerprint"),
        "decisions": list(state.decisions.values()),
        "decision_order": state.decision_order,
        "conflicts": list(state.conflicts.values()),
        "stale_runs": state.stale_results,
        "invalidations": state.invalidations,
        "events": state.events,
    }
