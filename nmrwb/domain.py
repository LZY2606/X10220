"""领域操作：提交输入版本、依赖失效、人工决定/冲突、重放。"""
import json

from . import parsing
from .engine import PARAM_FIELDS_USED
from .store import MAIN_BRANCH

PARAM_COMPARE = ["solvent", "temperature_K", "reference", "freq_mhz"]


def commit_input(store, kind, label, raw_text, actor, parent_id=None):
    """解析并提交一个不可变输入版本；修订通过 parent_id 形成版本链。"""
    if kind == "peaks1d":
        parsed = parsing.parse_peaks1d(raw_text)
    elif kind == "corr2d":
        parsed = parsing.parse_corr2d(raw_text)
    elif kind == "params":
        parsed = parsing.parse_params(raw_text)
    elif kind == "molecule":
        parsed = parsing.parse_molecule(raw_text)
    else:
        raise ValueError("unknown input kind: %s" % kind)

    inp = store.add_input(kind, label, raw_text, actor, parent_id)
    iid = inp["input_id"]
    if kind == "peaks1d":
        for p in parsed:
            p["peak_key"] = "%s:%s" % (iid, p["local_id"])
        store.add_peaks(iid, parsed)
        diags = {p["peak_key"]: p["diag"] for p in parsed if p["diag"]}
    elif kind == "corr2d":
        for e in parsed:
            e["edge_key"] = "%s:%s" % (iid, e["local_id"])
        store.add_edges(iid, parsed)
        diags = {e["edge_key"]: e["diag"] for e in parsed if e["diag"]}
    elif kind == "params":
        store.add_params(iid, parsed)
        diags = {iid: parsed["diag"]} if parsed["diag"] else {}
    else:
        name, atoms, bonds = parsed
        for a in atoms:
            a["atom_key"] = "%s:%s" % (iid, a["atom_id"])
        store.add_molecule(iid, name, atoms, bonds)
        diags = {}

    store.emit("input.committed", {
        "input_id": iid, "kind": kind, "version_no": inp["version_no"],
        "parent_id": parent_id, "diagnostics": diags,
    }, MAIN_BRANCH, actor)
    if parent_id:
        invalidate(store, inp, actor)
    return inp, diags


def _peak_sig(p):
    return (p["nucleus"], p["shift_raw"], p["intensity_raw"],
            p["integral_raw"], p["multiplicity"])


def _edge_sig(e):
    return (e["kind"], e["a"], e["b"], e["strength_raw"])


def invalidate(store, new_input, actor):
    """局部输入变化只使直接依赖它的候选过期；过期候选保留可查。"""
    parent = store.get_input(new_input["parent_id"])
    kind, staled = new_input["kind"], []
    if kind == "params":
        old, new = store.params_of(parent["input_id"]), \
                   store.params_of(new_input["input_id"])
        changed = [k for k in PARAM_COMPARE if old.get(k) != new.get(k)]
        relevant = [f for f in changed if f in PARAM_FIELDS_USED]
        if relevant:
            for c in store.candidates_all():
                if json.loads(c["sources"])["params"] == parent["input_id"]:
                    store.set_status(MAIN_BRANCH, c["cand_id"], "stale",
                                     "params changed: %s" % relevant)
                    staled.append(c["cand_id"])
        store.emit("judgment.stale_on_param_change", {
            "changed_fields": changed, "relevant_fields": relevant,
            "staled": staled,
            "rule": "溶剂/温度/参考标准变更仅使相关候选过期，旧决定仍可重放",
        }, MAIN_BRANCH, actor)
    elif kind in ("peaks1d", "corr2d"):
        if kind == "peaks1d":
            old_items = {p["local_id"]: _peak_sig(p)
                         for p in store.peaks_of(parent["input_id"])}
            new_items = {p["local_id"]: _peak_sig(p)
                         for p in store.peaks_of(new_input["input_id"])}
            field, prefix = "peaks", "peak"
        else:
            old_items = {e["local_id"]: _edge_sig(e)
                         for e in store.edges_of(parent["input_id"])}
            new_items = {e["local_id"]: _edge_sig(e)
                         for e in store.edges_of(new_input["input_id"])}
            field, prefix = "edges", "edge"
        changed_locals = [lid for lid in set(old_items) | set(new_items)
                          if old_items.get(lid) != new_items.get(lid)]
        old_keys = {"%s:%s" % (parent["input_id"], lid)
                    for lid in changed_locals}
        for c in store.candidates_all():
            src = json.loads(c["sources"])
            if set(src.get(field, [])) & old_keys:
                store.set_status(MAIN_BRANCH, c["cand_id"], "stale",
                                 "source %s changed: %s"
                                 % (kind, sorted(changed_locals)))
                staled.append(c["cand_id"])
        store.emit("judgment.stale_on_source_change", {
            "kind": kind, "changed_locals": sorted(changed_locals),
            "staled": staled,
            "rule": "局部源数据改动仅使依赖它的候选过期，旧值保留可重放",
        }, MAIN_BRANCH, actor)
    elif kind == "molecule":
        for c in store.candidates_all():
            if json.loads(c["sources"])["molecule"] == parent["input_id"]:
                store.set_status(MAIN_BRANCH, c["cand_id"], "stale",
                                 "molecule revised")
                staled.append(c["cand_id"])
        store.emit("judgment.stale_on_source_change", {
            "kind": kind, "staled": staled,
            "rule": "分子骨架修订使全部依赖候选过期",
        }, MAIN_BRANCH, actor)
    return staled


def decide(store, nucleus, cand_id, action, actor, note=None,
           branch_id=MAIN_BRANCH):
    """人工确认/否定：生成新分支，写入状态与传播影响，检测编辑者冲突。"""
    cand = store.get_candidate(cand_id)
    if not cand:
        raise ValueError("candidate not found: %s" % cand_id)
    if cand["nucleus"] != nucleus:
        raise ValueError("candidate %s belongs to nucleus %s"
                         % (cand_id, cand["nucleus"]))
    if action not in ("accept", "reject"):
        raise ValueError("action must be accept|reject")
    parent_br = store.get_branch(branch_id)
    if not parent_br:
        raise ValueError("branch not found: %s" % branch_id)

    new_br = store.fork_branch(branch_id,
                               name="%s:%s" % (action, nucleus))
    propagation = []
    if action == "accept":
        store.set_status(new_br["branch_id"], cand_id, "accepted",
                         "decision by %s" % actor)
        propagation.append({"cand_id": cand_id, "status": "accepted"})
        for sib in store.candidates_for_nucleus(nucleus):
            if sib["cand_id"] == cand_id:
                continue
            cur = store.resolve_status(sib, new_br["branch_id"])
            if cur in ("candidate", "tied"):
                store.set_status(new_br["branch_id"], sib["cand_id"],
                                 "superseded", "superseded by accept")
                propagation.append({"cand_id": sib["cand_id"],
                                    "status": "superseded"})
    else:
        store.set_status(new_br["branch_id"], cand_id, "rejected",
                         "decision by %s" % actor)
        propagation.append({"cand_id": cand_id, "status": "rejected"})
    dec = store.add_decision(new_br["branch_id"], nucleus, cand_id,
                             action, actor, note)
    ev = store.emit("decision", {
        "decision_id": dec["decision_id"], "nucleus": nucleus,
        "cand_id": cand_id, "action": action, "note": note,
        "propagation": propagation,
    }, new_br["branch_id"], actor)

    # 冲突检测：同一 fork 点的兄弟分支上，其他编辑者对同一核的不同决定
    conflict = None
    fork_event = parent_br["head_event"]
    for sb in store.branches_all():
        if sb["branch_id"] == new_br["branch_id"]:
            continue
        if sb["parent_branch"] == branch_id and sb["fork_event"] == fork_event:
            for d in store.decisions_on_branch(sb["branch_id"]):
                if d["nucleus"] == nucleus and \
                        (d["cand_id"] != cand_id or d["action"] != action):
                    conflict = store.emit("conflict", {
                        "nucleus": nucleus,
                        "decisions": [d["decision_id"], dec["decision_id"]],
                        "actors": [d["actor"], actor],
                        "branches": [sb["branch_id"], new_br["branch_id"]],
                        "rule": "两个编辑者冲突：双方决定都保留，需显式解决",
                    }, new_br["branch_id"], actor)
    return {"decision": dec, "branch": new_br, "event": ev,
            "propagation": propagation, "conflict": conflict}


def resolve_conflict(store, nucleus, chosen_decision_id, actor):
    chosen = store.get_decision(chosen_decision_id)
    if not chosen or chosen["nucleus"] != nucleus:
        raise ValueError("decision not found for nucleus")
    chosen_br = store.get_branch(chosen["branch_id"])
    superseded = []
    for sb in store.branches_all():
        if sb["parent_branch"] == chosen_br["parent_branch"] and \
                sb["fork_event"] == chosen_br["fork_event"] and \
                sb["branch_id"] != chosen["branch_id"]:
            for d in store.decisions_on_branch(sb["branch_id"]):
                if d["nucleus"] == nucleus:
                    superseded.append(d["decision_id"])
    return store.emit("conflict_resolved", {
        "nucleus": nucleus, "chosen": chosen_decision_id,
        "superseded_decisions": superseded,
        "rule": "冲突解决写入历史，被否决分支仍可重放",
    }, chosen["branch_id"], actor)


def replay(store, event_id):
    """回到任意旧事件：恢复当时的依赖版本与可见结论（不覆盖历史）。"""
    ev = store.get_event(event_id)
    if not ev:
        raise ValueError("event not found: %s" % event_id)
    s = ev["seq"]
    branch_id = ev["branch_id"] or MAIN_BRANCH
    inputs = [i for i in store.all_inputs() if i["seq"] <= s]
    heads = store.head_inputs(max_seq=s)
    cands = []
    for c in store.candidates_all():
        if c["created_seq"] <= s:
            row = dict(c)
            row["status"] = store.resolve_status(c, branch_id, max_seq=s)
            cands.append(row)
    return {
        "event": ev,
        "inputs": inputs,
        "head_input_ids": [h["input_id"] for h in heads],
        "candidates": cands,
        "decisions": [d for d in store.decisions_all() if d["seq"] <= s],
        "events": [e for e in store.events_all() if e["seq"] <= s],
    }
