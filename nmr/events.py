"""Event-sourced project state machine.

The append-only event log is the only source of truth. Every submission,
parameter revision, candidate run, human decision, conflict resolution and
branch merge is an event. Replaying events at any point restores the exact
dependency versions and visible conclusions of that moment; newer
computations never overwrite history.
"""
from __future__ import annotations

from copy import deepcopy

from .ident import event_id, fingerprint, stable_id

EVENT_KINDS = {
    "project_created", "source_submitted", "source_revised",
    "params_revised", "candidates_computed", "decision_made",
    "conflict_resolved", "branch_created",
}


def make_event(kind: str, payload: dict, *, seq: int, parent_event_id: str | None,
               branch: str, timestamp: str, editor: str,
               merge_parents: list[str] | None = None) -> dict:
    event = {
        "id": event_id(kind, payload, parent_event_id),
        "kind": kind,
        "seq": seq,
        "branch": branch,
        "editor": editor,
        "timestamp": timestamp,
        "parent_event_id": parent_event_id,
        "merge_parents": merge_parents or [],
        "payload": payload,
    }
    return event


class ReducerError(ValueError):
    pass


def initial_state() -> dict:
    return {
        "project_id": None,
        "title": None,
        "branches": {},
        "branch_heads": {},
        "events": [],
        "events_by_id": {},
        "sources": {},
        "source_chains": {},
        "skeleton_version": None,
        "params": None,
        "params_history": [],
        "runs": [],
        "run_index": 0,
        "decisions": [],
        "resolutions": [],
    }


def reduce_event(state: dict, event: dict) -> dict:
    state = deepcopy(state)
    kind = event["kind"]
    payload = event["payload"]
    if event["id"] in state["events_by_id"]:
        return state
    if state["project_id"] is not None and kind == "project_created":
        raise ReducerError("project already created")

    if kind == "project_created":
        state["project_id"] = payload["project_id"]
        state["title"] = payload.get("title", "Untitled")
        state["branches"] = {"main": {"name": "main", "created_at": event["timestamp"],
                                      "parent": None, "editor": event["editor"]}}
        state["branch_heads"] = {"main": event["id"]}
    elif kind == "source_submitted":
        _add_source_version(state, payload, first=True)
    elif kind == "source_revised":
        _add_source_version(state, payload, first=False)
    elif kind == "params_revised":
        state["params"] = deepcopy(payload["params"])
        state["params_history"].append({
            "params": deepcopy(payload["params"]),
            "event_id": event["id"],
            "timestamp": event["timestamp"],
            "note": payload.get("note", ""),
            "reason": payload.get("reason", ""),
        })
    elif kind == "candidates_computed":
        state["runs"].append(deepcopy(payload["run"]))
        state["run_index"] = max(state["run_index"], payload["run"]["run_index"])
    elif kind == "decision_made":
        state["decisions"].append(
            _decision_record(payload, event))
    elif kind == "conflict_resolved":
        state["resolutions"].append({
            "site_id": payload["site_id"],
            "resolution": payload["resolution"],
            "chosen_decision_id": payload.get("chosen_decision_id"),
            "editor": event["editor"],
            "timestamp": event["timestamp"],
            "event_id": event["id"],
            "branch": event["branch"],
            "note": payload.get("note", ""),
            "superseded": False,
        })
    elif kind == "branch_created":
        name = payload["name"]
        if name in state["branches"]:
            raise ReducerError(f"branch {name!r} already exists")
        state["branches"][name] = {
            "name": name,
            "created_at": event["timestamp"],
            "parent": payload.get("from_branch", "main"),
            "editor": event["editor"],
            "note": payload.get("note", ""),
        }
        state["branch_heads"][name] = event["id"]
    else:  # pragma: no cover - guarded on ingest
        raise ReducerError(f"unknown event kind {kind}")

    state["events"].append(event)
    state["events_by_id"][event["id"]] = event
    if event["branch"] in state["branches"]:
        state["branch_heads"][event["branch"]] = event["id"]
    if kind == "conflict_resolved":
        state["branch_heads"]["main"] = event["id"]
    return state


def _add_source_version(state: dict, payload: dict, *, first: bool) -> None:
    role = payload["role"]
    chain = state["source_chains"].setdefault(role, [])
    version = payload["version"]
    if any(v["version"] == version for v in chain):
        raise ReducerError(f"source version {version!r} for {role} already exists")
    if not first and not chain:
        raise ReducerError(f"cannot revise {role}: no parent version")
    record = {
        "role": role,
        "version": version,
        "parent_version": payload.get("parent_version"),
        "filename": payload["filename"],
        "raw_content": payload["raw_content"],
        "content_sha": payload["content_sha"],
        "parser_version": payload["parsed"]["parser_version"],
        "parsed": payload["parsed"],
        "submitted_by": payload.get("submitted_by", "unknown"),
        "event_note": payload.get("note", ""),
    }
    chain.append(record)
    state["sources"][role] = record
    if role == "skeleton":
        state["skeleton_version"] = version


def _decision_record(payload: dict, event: dict) -> dict:
    return {
        "decision_id": "dec_" + fingerprint(
            [payload["site_id"], payload["action"], payload.get("candidate_key")],
            length=14),
        "site_id": payload["site_id"],
        "action": payload["action"],
        "candidate_key": payload.get("candidate_key"),
        "run_id": payload["run_id"],
        "basis_versions": deepcopy(payload.get("basis_versions", {})),
        "conditions_snapshot": deepcopy(payload.get("conditions_snapshot", {})),
        "params_fingerprint": payload.get("params_fingerprint"),
        "editor": event["editor"],
        "timestamp": event["timestamp"],
        "event_id": event["id"],
        "branch": event["branch"],
        "note": payload.get("note", ""),
        "superseded": False,
    }


def replay(events: list[dict]) -> dict:
    state = initial_state()
    for event in sorted(events, key=lambda e: e["seq"]):
        state = reduce_event(state, event)
    return state


def visible_event_ids(state: dict, branch: str) -> list[str]:
    """Linear ancestry of a branch head, following parents and merge parents."""
    if branch not in state["branches"]:
        raise ReducerError(f"unknown branch {branch!r}")
    head = state["branch_heads"].get(branch)
    ordered = list(state["events"])
    by_id = state["events_by_id"]
    reachable: set[str] = set()
    stack = [head]
    while stack:
        current = stack.pop()
        if not current or current in reachable:
            continue
        reachable.add(current)
        event = by_id[current]
        if event["parent_event_id"]:
            stack.append(event["parent_event_id"])
        stack.extend(event.get("merge_parents", []))
    return [event["id"] for event in ordered if event["id"] in reachable]
