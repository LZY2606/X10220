"""Projection: derive the visible state by replaying the event chain.

Everything the UI shows is a pure function of the event chain up to the
checked-out event, so returning to any historical event restores the
dependency versions and visible conclusions of that moment instead of
overwriting history with fresh computation.
"""
from . import engine


def empty_state(project_id):
    return {
        "project_id": project_id,
        "inputs": {},            # kind -> {input_id, version, parent_id}
        "context": {},           # solvent / instrument_mhz / temperature_K / reference
        "candidates": [],
        "compute": None,         # fingerprint/algo/params of the active candidate set
        "decisions": {},         # candidate_id -> [ {verdict, actor, seq} ]
        "conflicts": [],         # unresolved or resolved conflict records
        "stale": {},             # candidate_id -> reason
        "displaced": {},         # candidate_id -> reason (propagated by a confirm)
        "events": [],
        "tip_seq": 0,
    }


def _candidate_id(fingerprint, atom_id, peak_key):
    return "cand-%s-%s-%s" % (fingerprint[:8], atom_id, peak_key)


def replay(store, project_id, at_seq=None):
    proj = store.get_project(project_id)
    if proj is None:
        raise KeyError("unknown project %s" % project_id)
    tip = proj["active_tip"] if at_seq is None else at_seq
    state = empty_state(project_id)
    state["tip_seq"] = tip
    if not tip:
        return state
    inputs_by_id = {i["id"]: i for i in store.inputs_of(project_id)}
    undone = set()
    for ev in store.chain_to(tip):
        kind = ev["kind"]
        payload = ev["payload"]
        state["events"].append({
            "seq": ev["seq"], "kind": kind, "actor": ev["actor"],
            "branch": ev["branch"], "ts": ev["ts"],
            "summary": payload.get("summary", ""),
        })
        if kind == "undo":
            undone.add(payload["target_seq"])
            _rebuild_decisions(state)
            continue
        if kind == "import":
            state["inputs"][payload["kind"]] = {
                "input_id": payload["input_id"],
                "version": payload["version"],
                "parent_id": payload.get("parent_id"),
            }
            rec = inputs_by_id.get(payload["input_id"])
            if rec and payload["kind"] in ("peaks-1h", "peaks-13c", "peaks-2d", "context"):
                ctx = rec["content"].get("context") or {}
                state["context"].update(ctx)
            _expire_by_input(state, payload["kind"], payload["input_id"])
        elif kind == "context":
            state["context"].update(payload["context"])
            affected = payload.get("affected_nuclei") or []
            for cand in state["candidates"]:
                if cand["nucleus"] in affected:
                    state["stale"][cand["candidate_id"]] = (
                        "context changed (%s); candidate expired, old decisions remain replayable"
                        % payload.get("reason", "unspecified"))
        elif kind == "compute":
            fp = payload["fingerprint"]
            cands = []
            for c in payload["candidates"]:
                cand = dict(c)
                cand["candidate_id"] = _candidate_id(fp, c["atom_id"], c["peak_key"])
                cand["fingerprint"] = fp
                cands.append(cand)
            state["candidates"] = cands
            state["displaced"] = {}
            state["compute"] = {
                "fingerprint": fp,
                "algo_version": payload["algo_version"],
                "params": payload["params"],
                "input_ids": payload["input_ids"],
                "overlap_groups": payload.get("overlap_groups", {}),
                "seq": ev["seq"],
            }
            state["stale"] = {}
        elif kind == "decide":
            cid = payload["candidate_id"]
            entry = {"verdict": payload["verdict"], "actor": ev["actor"],
                     "seq": ev["seq"]}
            prior = [d for d in state["decisions"].get(cid, [])
                     if d["seq"] not in undone]
            if prior and prior[-1]["verdict"] != payload["verdict"] \
                    and prior[-1]["actor"] != ev["actor"]:
                state["conflicts"].append({
                    "candidate_id": cid,
                    "status": "open",
                    "parties": [
                        {"actor": prior[-1]["actor"], "verdict": prior[-1]["verdict"],
                         "seq": prior[-1]["seq"]},
                        {"actor": ev["actor"], "verdict": payload["verdict"],
                         "seq": ev["seq"]},
                    ],
                })
            state["decisions"].setdefault(cid, []).append(entry)
            if payload["verdict"] == "confirmed":
                for item in payload.get("impact", []):
                    if item["effect"] == "displaced":
                        state["displaced"][item["candidate_id"]] = item["reason"]
        elif kind == "resolve":
            cid = payload["candidate_id"]
            for conflict in state["conflicts"]:
                if conflict["candidate_id"] == cid and conflict["status"] == "open":
                    conflict["status"] = "resolved"
                    conflict["resolution"] = {
                        "chosen_verdict": payload["chosen_verdict"],
                        "actor": ev["actor"], "seq": ev["seq"],
                        "note": payload.get("note", ""),
                    }
            state["decisions"].setdefault(cid, []).append(
                {"verdict": payload["chosen_verdict"], "actor": ev["actor"],
                 "seq": ev["seq"]})
    _apply_statuses(state, undone)
    return state


def _rebuild_decisions(state):
    # decisions are re-filtered against `undone` in _apply_statuses; nothing else needed
    pass


def _expire_by_input(state, kind, new_input_id):
    for cand in state["candidates"]:
        for src in cand.get("sources", []):
            if src.startswith("in-%s-v" % kind) and src != new_input_id:
                state["stale"][cand["candidate_id"]] = (
                    "input %s superseded by %s; candidate expired but stays inspectable"
                    % (src, new_input_id))


def _apply_statuses(state, undone):
    for cand in state["candidates"]:
        cid = cand["candidate_id"]
        decisions = [d for d in state["decisions"].get(cid, [])
                     if d["seq"] not in undone]
        if cid in state["stale"]:
            cand["status"] = "stale"
            cand["stale_reason"] = state["stale"][cid]
        elif decisions:
            cand["status"] = decisions[-1]["verdict"]
        elif cid in state["displaced"]:
            cand["status"] = "rejected-by-choice"
            cand["displaced_reason"] = state["displaced"][cid]
        else:
            cand["status"] = "pending"
        cand["decision_history"] = decisions
    state["open_conflicts"] = [c for c in state["conflicts"] if c["status"] == "open"]


def impact_of_decision(state, candidate_id, verdict):
    """Which currently-visible candidates a confirm/reject would affect."""
    target = None
    for cand in state["candidates"]:
        if cand["candidate_id"] == candidate_id:
            target = cand
            break
    if target is None:
        return []
    impact = []
    if verdict == "confirmed":
        for cand in state["candidates"]:
            if cand["candidate_id"] == candidate_id or cand["status"] != "pending":
                continue
            if cand["atom_id"] == target["atom_id"]:
                impact.append({
                    "candidate_id": cand["candidate_id"],
                    "effect": "displaced",
                    "reason": "same atom %s receives a confirmed assignment"
                              % target["atom_id"],
                })
            elif cand["peak_key"] == target["peak_key"]:
                if "overlap" in cand["flags"] or "overlap" in target["flags"]:
                    impact.append({
                        "candidate_id": cand["candidate_id"],
                        "effect": "kept",
                        "reason": "peak %s is overlapped; unique match not enforced"
                                  % target["peak_key"],
                    })
                elif "symmetric" in cand["flags"]:
                    impact.append({
                        "candidate_id": cand["candidate_id"],
                        "effect": "kept",
                        "reason": "symmetric atoms share peaks legitimately",
                    })
                else:
                    impact.append({
                        "candidate_id": cand["candidate_id"],
                        "effect": "displaced",
                        "reason": "peak %s is consumed by the confirmed assignment"
                                  % target["peak_key"],
                    })
    return impact
