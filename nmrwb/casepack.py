"""离线案例包 "NMR 共振经纬" 的加载与叙事执行。

按 scenario.json 的顺序实际经过：基线导入 → 候选计算（重叠峰/对称原子/
缺失相关边/并列最优）→ 参数修订 → 局部源数据改动 → 化学位移整体偏置 →
两个编辑者冲突 → 冲突解决。每一步都留下机器可读事件。
"""
import json
from pathlib import Path

from . import domain
from .engine import run_compute
from .store import MAIN_BRANCH

CASE_DIR = Path(__file__).resolve().parent.parent / "casepack"


def _read(name):
    return (CASE_DIR / name).read_text(encoding="utf-8")


def load_scenario():
    return json.loads(_read("scenario.json"))


def samples():
    """供导入面板复制粘贴的小样例。"""
    return {
        "molecule": _read("molecule.json"),
        "params": _read("params_v1.txt"),
        "peaks1d": _read("peaks1d_v1.txt"),
        "corr2d": _read("corr2d_v1.txt"),
    }


def _find_current_candidate(store, nucleus, peak_local):
    for c in store.candidates_for_nucleus(nucleus):
        if not c["peak_key"]:
            continue
        if c["peak_key"].endswith(":" + peak_local) and \
                store.resolve_status(c, MAIN_BRANCH) in ("candidate", "tied"):
            return c
    raise ValueError("no current candidate for %s -> %s"
                     % (nucleus, peak_local))


def run_casepack(store):
    if not store.is_empty():
        raise ValueError("casepack requires an empty database")
    scenario = load_scenario()
    store.emit("casepack.baseline", {
        "title": scenario["title"], "narrative": scenario["narrative"],
        "rule": "化学位移/峰强/积分/多重性/相关边/溶剂/仪器频率按原文件与版本保留",
    }, MAIN_BRANCH, "casepack")
    by_file, log = {}, []
    for step in scenario["steps"]:
        op = step["op"]
        if op == "import":
            parent = by_file.get(step["parent_file"]) if step.get("parent_file") else None
            inp, diags = domain.commit_input(
                store, step["kind"], step["label"], _read(step["file"]),
                step["actor"], parent_id=parent["input_id"] if parent else None)
            by_file[step["file"]] = inp
            log.append({"op": op, "input_id": inp["input_id"],
                        "version_no": inp["version_no"], "diags": diags})
        elif op == "compute":
            summary = run_compute(store, MAIN_BRANCH, step["actor"])
            log.append({"op": op, "run_id": summary["run_id"]})
        elif op == "decide":
            cand = _find_current_candidate(store, step["nucleus"],
                                           step["peak_local"])
            res = domain.decide(store, step["nucleus"], cand["cand_id"],
                                step["action"], step["actor"],
                                note=step.get("note"), branch_id=MAIN_BRANCH)
            by_file["decision:%s" % step["actor"]] = res["decision"]
            log.append({"op": op, "decision_id": res["decision"]["decision_id"],
                        "branch": res["branch"]["branch_id"],
                        "conflict": bool(res["conflict"])})
        elif op == "resolve":
            chosen = by_file["decision:%s" % step["chosen_actor"]]
            ev = domain.resolve_conflict(store, step["nucleus"],
                                         chosen["decision_id"], step["actor"])
            log.append({"op": op, "event_id": ev["event_id"]})
    store.emit("casepack.complete", {
        "log": log,
        "rule": "被采纳与未采纳的结果都保留；可从最终状态反查材料/规则版本/决定",
    }, MAIN_BRANCH, "casepack")
    return log
