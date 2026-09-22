"""离线案例包加载器：按 scenario.json 回放“NMR 共振经纬”完整叙事。

步骤覆盖：基线导入 -> 参数修订 -> 只影响局部依赖的源数据改动 ->
参考标准变更（整体位移偏置）-> 两个编辑者冲突 -> 冲突解决与撤销。
"""
from __future__ import annotations

import json
import os
from typing import Dict

from . import services
from .core import Store


def _latest_current_set(state):
    sets = [s for s in state["candidate_sets"].values() if not s["stale"]]
    return sets[-1] if sets else None


def _pick_candidate(state, atom_id, pick):
    aset = _latest_current_set(state)
    if aset is None:
        raise ValueError("没有可用的当前候选集")
    cs = [c for c in aset["candidates"] if c["atom_id"] == atom_id]
    if not cs:
        raise ValueError("原子 %s 没有候选" % atom_id)
    if pick == "best":
        best = [c for c in cs if c["status"] == "best"]
        return sorted(best or cs, key=lambda c: c["candidate_id"])[0]
    alt = [c for c in cs if c["status"] != "best"]
    return sorted(alt or cs, key=lambda c: c["candidate_id"])[0]


def load_casepack(store: Store, casepack_dir: str) -> Dict:
    with open(os.path.join(casepack_dir, "scenario.json"),
              encoding="utf-8") as f:
        scenario = json.load(f)
    decision_by_editor = {}
    steps_done = 0
    for step in scenario["steps"]:
        op = step["op"]
        if op == "input":
            with open(os.path.join(casepack_dir, step["file"]),
                      encoding="utf-8") as f:
                raw = f.read()
            services.submit_input(store, step["name"], step["format"], raw)
        elif op == "params":
            services.revise_params(store, step["updates"])
        elif op == "compute":
            services.compute(store)
        elif op == "decide":
            state = services.current_state(store)
            cand = _pick_candidate(state, step["atom_id"], step["pick"])
            ev = services.decide(store, step["atom_id"], cand["candidate_id"],
                                 step["action"], step["editor"])
            decision_by_editor[step["editor"]] = ev["payload"]["decision_id"]
        elif op == "resolve_conflict":
            state = services.current_state(store)
            for c in state["conflicts"].values():
                if c["status"] == "open":
                    services.resolve_conflict(
                        store, c["conflict_id"],
                        decision_by_editor[step["winner"]], step["editor"])
        elif op == "undo":
            services.undo(store, decision_by_editor[step["target"]],
                          step["editor"])
        else:
            raise ValueError("未知案例步骤: %s" % op)
        steps_done += 1
    return {"case_id": scenario["case_id"], "title": scenario["title"],
            "steps": steps_done}
