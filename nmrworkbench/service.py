"""Application service layer shared by the HTTP API and the scenario runner."""
from . import engine, importer, projection

INPUT_KINDS = ("peaks-1h", "peaks-13c", "peaks-2d", "context")


def import_text(store, project_id, kind, text, source, actor="reviewer"):
    content, errors = importer.parse_text(text)
    if errors:
        raise importer.ImportError_(errors)
    rec = store.put_input(project_id, kind, content, source=source)
    store.append_event(project_id, "import", {
        "input_id": rec["id"], "kind": kind, "version": rec["version"],
        "parent_id": rec["parent_id"], "source": source,
        "summary": "导入 %s v%d（%s），原始表示按原文件保留" % (kind, rec["version"], source),
    }, actor=actor)
    return rec


def _current_inputs(store, project_id):
    latest = {}
    for rec in store.inputs_of(project_id):
        latest[rec["kind"]] = rec
    return latest


def run_compute(store, project_id, compound, params=None, actor="system"):
    latest = _current_inputs(store, project_id)
    peaks, edges, input_ids = [], [], []
    for kind in ("peaks-1h", "peaks-13c", "peaks-2d"):
        rec = latest.get(kind)
        if not rec:
            continue
        input_ids.append(rec["id"])
        for pk in rec["content"].get("peaks", []):
            peaks.append(dict(pk, input_id=rec["id"]))
        for eg in rec["content"].get("edges", []):
            edges.append(dict(eg, input_id=rec["id"]))
    params = dict(engine.DEFAULT_PARAMS, **(params or {}))
    fp = engine.fingerprint(params, input_ids)
    atoms = [a for a in compound["atoms"] if a.get("nucleus")]
    candidates, meta = engine.compute_candidates(atoms, peaks, edges, params)
    direct = {}
    for pk in peaks:
        direct.setdefault(pk["peak_key"], set()).add(pk["input_id"])
    for eg in edges:
        for key in (eg["a_key"], eg["b_key"]):
            direct.setdefault(key, set()).add(eg["input_id"])
    for cand in candidates:
        cand["sources"] = sorted(direct.get(cand["peak_key"], set()))
    ev = store.append_event(project_id, "compute", {
        "fingerprint": fp,
        "algo_version": engine.ALGO_VERSION,
        "params": params,
        "input_ids": input_ids,
        "candidates": candidates,
        "overlap_groups": meta["overlap_groups"],
        "summary": "重算候选：%d 个候选，算法 %s，参数指纹 %s"
                   % (len(candidates), engine.ALGO_VERSION, fp),
    }, actor=actor)
    return {"event": ev, "fingerprint": fp, "candidates": candidates,
            "overlap_groups": meta["overlap_groups"]}


def decide(store, project_id, candidate_id, verdict, actor):
    if verdict not in ("confirmed", "rejected"):
        raise ValueError("verdict must be 'confirmed' or 'rejected'")
    state = projection.replay(store, project_id)
    impact = projection.impact_of_decision(state, candidate_id, verdict)
    ev = store.append_event(project_id, "decide", {
        "candidate_id": candidate_id, "verdict": verdict, "impact": impact,
        "summary": "%s %s %s（影响 %d 个候选）" % (
            actor, "确认" if verdict == "confirmed" else "否定",
            candidate_id, len(impact)),
    }, actor=actor)
    return {"event": ev, "impact": impact}


def resolve(store, project_id, candidate_id, chosen_verdict, actor, note=""):
    ev = store.append_event(project_id, "resolve", {
        "candidate_id": candidate_id, "chosen_verdict": chosen_verdict, "note": note,
        "summary": "%s 解决冲突 %s -> %s" % (actor, candidate_id, chosen_verdict),
    }, actor=actor)
    return {"event": ev}


def undo(store, project_id, target_seq, actor):
    target = store.get_event(target_seq)
    if target is None:
        raise KeyError("unknown event seq %s" % target_seq)
    ev = store.append_event(project_id, "undo", {
        "target_seq": target_seq,
        "summary": "%s 撤销事件 #%d（%s）" % (actor, target_seq, target["kind"]),
    }, actor=actor)
    return {"event": ev}


def change_context(store, project_id, context, affected_nuclei, reason, actor):
    rec = store.put_input(project_id, "context",
                          {"context": dict(context), "peaks": [], "edges": [],
                           "raw_text": "", "diagnostics": []},
                          source="context-change")
    store.append_event(project_id, "import", {
        "input_id": rec["id"], "kind": "context", "version": rec["version"],
        "parent_id": rec["parent_id"], "source": "context-change",
        "summary": "上下文版本 v%d 存档" % rec["version"],
    }, actor=actor)
    ev = store.append_event(project_id, "context", {
        "context": dict(context), "affected_nuclei": list(affected_nuclei),
        "reason": reason,
        "summary": "上下文变更（%s）：%s；仅使相关候选过期" % (reason, context),
    }, actor=actor)
    return {"event": ev}


def apply_patch(content, patch):
    """Apply a counterexample patch to a copy of an input's content."""
    import copy
    content = copy.deepcopy(content)
    peaks = content.get("peaks", [])
    edges = content.get("edges", [])
    for key in patch.get("remove_peaks", []):
        peaks = [p for p in peaks if p["peak_key"] != key]
    for spec in patch.get("add_peaks", []):
        def _num(raw):
            try:
                return float(raw) if raw not in ("", None) else None
            except (TypeError, ValueError):
                return None
        peaks.append({
            "peak_key": spec["peak_key"], "nucleus": spec["nucleus"],
            "shift_ppm": _num(spec.get("shift_raw")),
            "shift_raw": str(spec.get("shift_raw", "")),
            "intensity": _num(spec.get("intensity_raw")),
            "intensity_raw": str(spec.get("intensity_raw", "")),
            "integration": _num(spec.get("integration_raw")),
            "integration_raw": str(spec.get("integration_raw", "")),
            "multiplicity": spec.get("multiplicity") or None,
            "unit": "ppm", "line": 0,
        })
    shift_spec = patch.get("shift_ppm")
    if shift_spec:
        delta = float(shift_spec["delta"])
        for p in peaks:
            if p["nucleus"] == shift_spec["nucleus"] and p["shift_ppm"] is not None:
                p["shift_ppm"] = round(p["shift_ppm"] + delta, 6)
                p["shift_raw"] = repr(p["shift_ppm"])
    for rem in patch.get("remove_edges", []):
        edges = [e for e in edges if not (
            e["edge_type"] == rem["edge_type"]
            and {e["a_key"], e["b_key"]} == {rem["a_key"], rem["b_key"]})]
    content["peaks"] = peaks
    content["edges"] = edges
    if patch.get("set_context"):
        content.setdefault("context", {}).update(patch["set_context"])
    return content
