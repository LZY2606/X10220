"""离线案例包「NMR 共振经纬」的装载与重放。

重放脚本通过 Workbench 的真实事务路径执行，实际经过
重叠峰、对称原子、缺失相关边、化学位移整体偏置、并列最优候选与两编辑者冲突。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .domain import candidate_id
from .service import Workbench

CASE_DIR = Path(__file__).resolve().parent.parent / "casework" / "nmr_resonance_atlas"


def load_case(case_dir: Path = CASE_DIR) -> dict:
    return {
        "baseline": json.loads((case_dir / "baseline.json").read_text(encoding="utf-8")),
        "counterexamples": json.loads((case_dir / "counterexamples.json").read_text(encoding="utf-8")),
        "replay": json.loads((case_dir / "replay_log.json").read_text(encoding="utf-8")),
    }


def apply_mutation(base_data: dict, mutation: dict) -> dict:
    """对基线内容施加单点变异，返回新内容（原始基线不被修改）。"""
    data = copy.deepcopy(base_data)
    op = mutation["op"]
    if op == "set_peak_shift":
        for p in data["peaks"]:
            if p["peak_id"] == mutation["peak_id"]:
                p["shift"] = {"value": mutation["value"], "unit": "ppm", "reference": "TMS"}
    elif op == "offset_all_shifts":
        for p in data["peaks"]:
            if p["shift"].get("unit") == "ppm":
                p["shift"]["value"] = round(p["shift"]["value"] + mutation["delta_ppm"], 4)
    elif op == "remove_edge":
        data["edges"] = [e for e in data["edges"] if e["edge_id"] != mutation["edge_id"]]
    elif op == "add_edge":
        data["edges"].append(mutation["edge"])
    elif op == "patch_carbon_site":
        for site in data["carbon_sites"]:
            if site["atom_id"] == mutation["atom_id"]:
                site[mutation["key"]] = mutation["value"]
    elif op == "replace_data":
        data = copy.deepcopy(mutation["data"])
    else:
        raise ValueError(f"未知变异操作 {op!r}")
    return data


def seed(workbench: Workbench, case_dir: Path = CASE_DIR) -> dict:
    """执行完整重放脚本，返回关键节点的事件/对象身份，供测试与界面反查。"""
    case = load_case(case_dir)
    baseline = case["baseline"]
    cx_entries = {e["id"]: e for e in case["counterexamples"]["entries"]}
    wb = workbench
    slots: dict[str, str] = {}
    report: dict = {"decisions": {}, "conflicts": {}, "counterexamples": {}, "events": {}}

    for step in case["replay"]["steps"]:
        op = step["op"]
        actor = step.get("actor", "system")
        if op == "project":
            ev = wb.create_project(baseline["title"], baseline["case_id"], actor)
            report["events"]["project"] = ev["event_id"]
        elif op == "commit_inputs":
            for stream, spec in baseline["inputs"].items():
                wb.commit_input(stream, spec["data"], spec["source_file"], actor)
        elif op == "commit_params":
            wb.commit_params(baseline["params"], actor)
        elif op == "judgment":
            wb.record_judgment(step["judgment"], actor)
        elif op == "regenerate":
            wb.regenerate(actor)
        elif op == "decide":
            cid = candidate_id(step["kind"], step["atom"], step["peak"])
            res = wb.decide(cid, step["verdict"], actor, step.get("rationale", ""))
            slots[step["as"]] = res["decision_id"]
            report["decisions"][step["as"]] = res["decision_id"]
        elif op == "revise_params":
            wb.revise_params(step["patch"], step["changed_keys"], actor, step.get("note", ""))
        elif op == "revise_input":
            st = wb.state()
            base = baseline["inputs"][step["stream"]]["data"] if step.get("from_baseline", True) \
                else st["inputs"][step["stream"]]["data"]
            new_data = apply_mutation(base, step["mutation"])
            wb.revise_input(step["stream"], new_data, None, actor, step.get("note", ""))
        elif op == "counterexample":
            entry = cx_entries[step["id"]]
            if entry["mutation"]:
                stream = entry["mutation"]["stream"]
                new_data = apply_mutation(baseline["inputs"][stream]["data"], entry["mutation"])
                rev = wb.revise_input(stream, new_data, None, actor,
                                      f"反例 {entry['id']}：{entry['title']}")
                regen = wb.regenerate(actor)
                report["counterexamples"][entry["id"]] = {
                    "revision_event": rev["event"]["event_id"],
                    "impact": rev["impact"],
                    "regen_event": regen["event"]["event_id"],
                }
            for judgment in entry["judgments"]:
                wb.record_judgment({**judgment, "counterexample": entry["id"]}, actor)
        elif op == "conflict":
            cid = candidate_id(step["kind"], step["atom"], step["peak"])
            fork = wb.state()["head_event"]
            r1 = wb.decide(cid, "confirm", step["confirm_by"], step.get("confirm_rationale", ""), from_event=fork)
            r2 = wb.decide(cid, "reject", step["reject_by"], step.get("reject_rationale", ""), from_event=fork)
            slots[step["as"] + ":confirm"] = r1["decision_id"]
            slots[step["as"] + ":reject"] = r2["decision_id"]
            report["conflicts"][step["as"]] = {
                "confirm_decision": r1["decision_id"],
                "reject_decision": r2["decision_id"],
                "conflict": r2.get("conflict"),
            }
        elif op == "resolve_conflict":
            st = wb.state()
            open_conflicts = [c for c in st.get("conflicts", {}).values() if not c["resolved"]]
            if open_conflicts:
                conf = open_conflicts[-1]
                chosen = None
                for side in conf["sides"]:
                    if side["verdict"] == step["side"]:
                        chosen = side["decision_id"]
                wb.resolve_conflict(conf["conflict_id"], chosen, actor, step.get("rationale", ""))
        elif op == "undo":
            wb.revert_decision(slots[step["target"]], actor, step.get("rationale", ""))
        else:
            raise ValueError(f"未知重放步骤 {op!r}")
    return report
