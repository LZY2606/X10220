"""Scenario runner: applies counterexample mutations as event-chain steps and
evaluates the machine-readable expectations archived in the case package."""
from . import casepack, projection, service
from .projection import _candidate_id


def _find_candidate(state, atom_id, peak_key):
    for cand in state["candidates"]:
        if cand["atom_id"] == atom_id and cand["peak_key"] == peak_key:
            return cand
    return None


def _dangling_edges(store, project_id):
    latest = {}
    for rec in store.inputs_of(project_id):
        latest[rec["kind"]] = rec
    known = set()
    for kind in ("peaks-1h", "peaks-13c"):
        rec = latest.get(kind)
        if rec:
            known |= {p["peak_key"] for p in rec["content"].get("peaks", [])}
    dangling = []
    rec2d = latest.get("peaks-2d")
    if rec2d:
        for e in rec2d["content"].get("edges", []):
            for key in (e["a_key"], e["b_key"]):
                if key not in known:
                    dangling.append({"edge_type": e["edge_type"], "a_key": e["a_key"],
                                     "b_key": e["b_key"], "missing": key,
                                     "input_id": rec2d["id"]})
    return dangling


def run_scenario(store, project_id, compound, name):
    spec = casepack.load_scenario(name)
    steps_out = []
    for step in spec["steps"]:
        op = step["op"]
        if op == "revise_input":
            prev = None
            for rec in store.inputs_of(project_id):
                if rec["kind"] == step["kind"]:
                    prev = rec
            if prev is None:
                raise KeyError("no input of kind %s to revise" % step["kind"])
            content = service.apply_patch(prev["content"], step["patch"])
            rec = store.put_input(project_id, step["kind"], content,
                                  source="counterexample:%s" % name)
            ev = store.append_event(project_id, "import", {
                "input_id": rec["id"], "kind": step["kind"], "version": rec["version"],
                "parent_id": rec["parent_id"], "source": "counterexample:%s" % name,
                "summary": "反例[%s]：修订 %s -> v%d（父版本 %s）"
                           % (name, step["kind"], rec["version"], rec["parent_id"]),
            }, actor="scenario")
            steps_out.append({"op": op, "event_seq": ev["seq"], "input_id": rec["id"]})
        elif op == "compute":
            res = service.run_compute(store, project_id, compound, actor="scenario")
            steps_out.append({"op": op, "event_seq": res["event"]["seq"],
                              "fingerprint": res["fingerprint"],
                              "n_candidates": len(res["candidates"])})
        elif op == "decide":
            state = projection.replay(store, project_id)
            fp = state["compute"]["fingerprint"]
            cid = _candidate_id(fp, step["atom_id"], step["peak_key"])
            res = service.decide(store, project_id, cid, step["verdict"],
                                 actor=step["actor"])
            steps_out.append({"op": op, "event_seq": res["event"]["seq"],
                              "candidate_id": cid, "impact": res["impact"]})
        elif op == "resolve":
            state = projection.replay(store, project_id)
            fp = state["compute"]["fingerprint"]
            cid = _candidate_id(fp, step["atom_id"], step["peak_key"])
            res = service.resolve(store, project_id, cid, step["chosen_verdict"],
                                  actor=step["actor"], note=step.get("note", ""))
            steps_out.append({"op": op, "event_seq": res["event"]["seq"]})
        elif op == "context":
            res = service.change_context(store, project_id, step["context"],
                                         step["affected_nuclei"], step["reason"],
                                         actor="scenario")
            steps_out.append({"op": op, "event_seq": res["event"]["seq"]})
        else:
            raise ValueError("unknown scenario op %s" % op)
    checks = check_expectations(store, project_id, spec)
    return {"scenario": name, "title": spec["title"], "steps": steps_out,
            "checks": checks}


def check_expectations(store, project_id, spec):
    state = projection.replay(store, project_id)
    results = []
    for exp in spec.get("expectations", []):
        kind = exp["assert"]
        ok, detail = False, ""
        if kind == "flag_present":
            cand = _find_candidate(state, exp["atom_id"], exp["peak_key"])
            ok = bool(cand and exp["flag"] in cand["flags"])
            detail = "flags=%s" % (cand["flags"] if cand else "candidate-missing")
        elif kind == "status":
            cand = _find_candidate(state, exp["atom_id"], exp["peak_key"])
            ok = bool(cand and cand["status"] == exp["status"])
            detail = "status=%s" % (cand["status"] if cand else "candidate-missing")
        elif kind == "not_displaced_by":
            cand = _find_candidate(state, exp["atom_id"], exp["peak_key"])
            ok = bool(cand and cand["status"] != "rejected-by-choice")
            detail = "status=%s" % (cand["status"] if cand else "candidate-missing")
        elif kind == "impact_kept":
            cand = _find_candidate(state, exp["atom_id"], exp["peak_key"])
            kept = False
            if cand:
                for ev in reversed(state["events"]):
                    full = store.get_event(ev["seq"])
                    if full["kind"] == "decide":
                        kept = any(i["candidate_id"] == cand["candidate_id"]
                                   and i["effect"] == "kept"
                                   for i in full["payload"].get("impact", []))
                        break
            ok = kept
            detail = "kept-in-impact=%s" % kept
        elif kind == "evidence_gap":
            cand = _find_candidate(state, exp["atom_id"], exp["peak_key"])
            ok = bool(cand and any(r["term"] == "evidence"
                                   and "evidence gap" in (r["detail"] or "")
                                   for r in cand["reasons"]))
            detail = "evidence-gap-recorded=%s" % ok
        elif kind == "replayable":
            cand = _find_candidate(state, exp["atom_id"], exp["peak_key"])
            ok = bool(cand and cand["decision_history"])
            if ok and cand:
                target_seq = cand["decision_history"][0]["seq"]
                hist = projection.replay(store, project_id, at_seq=target_seq)
                old = _find_candidate(hist, exp["atom_id"], exp["peak_key"])
                ok = bool(old and old["status"] ==
                          cand["decision_history"][0]["verdict"])
                detail = "historical-status=%s" % (old["status"] if old else "missing")
        elif kind == "tied":
            tied = [c for c in state["candidates"]
                    if c["atom_id"] == exp["atom_id"] and "tied" in c["flags"]]
            ok = len(tied) >= exp["count"]
            detail = "tied=%d" % len(tied)
        elif kind == "diagnostic_present":
            dangling = _dangling_edges(store, project_id)
            ok = len(dangling) > 0
            detail = "dangling=%s" % dangling
        elif kind == "conflict_recorded":
            cand = _find_candidate(state, exp["atom_id"], exp["peak_key"])
            ok = bool(cand and any(c["candidate_id"] == cand["candidate_id"]
                                   for c in state["conflicts"]))
            detail = "conflicts=%d" % len(state["conflicts"])
        elif kind == "conflict_resolved":
            cand = _find_candidate(state, exp["atom_id"], exp["peak_key"])
            ok = bool(cand and any(
                c["candidate_id"] == cand["candidate_id"] and c["status"] == "resolved"
                and c["resolution"]["chosen_verdict"] == exp["chosen_verdict"]
                for c in state["conflicts"]))
            detail = "resolved=%s" % ok
        else:
            detail = "unknown assertion"
        results.append({"assert": kind, "ok": ok, "detail": detail,
                        "basis": exp.get("basis", "")})
    return results

