"""应用服务：数据库引导、离线案例包、导出/导入。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .domain.engine import Engine
from .domain.importer import FormatError, parse_source_text
from .storage import Storage

BUNDLE_FORMAT = "nmr-evidence-room-bundle/1"
DEFAULT_DB = os.environ.get("NMR_DB", os.path.join(os.getcwd(), "data", "nmr_workbench.db"))
CASES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cases")


def open_engine(db_path: str = DEFAULT_DB, clock=None, seed: int = 20260922) -> Engine:
    return Engine(Storage(db_path), clock=clock, seed=seed)


def list_cases(cases_dir: str = CASES_DIR) -> List[Dict[str, Any]]:
    out = []
    if not os.path.isdir(cases_dir):
        return out
    for name in sorted(os.listdir(cases_dir)):
        manifest_path = os.path.join(cases_dir, name, "manifest.json")
        if os.path.isfile(manifest_path):
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
            out.append({"case_id": name, "title": manifest.get("title"),
                        "description": manifest.get("description"),
                        "steps": len(manifest.get("playbook", []))})
    return out


def read_case(case_id: str, cases_dir: str = CASES_DIR) -> Dict[str, Any]:
    case_dir = os.path.join(cases_dir, case_id)
    with open(os.path.join(case_dir, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    return {"case_dir": case_dir, "manifest": manifest}


def submit_case_sources(engine: Engine, case_id: str, project_id: str,
                        version: Optional[int] = None,
                        actor: str = "alice") -> List[Dict[str, Any]]:
    """按 manifest 顺序提交案例源文件（原始顺序即提交顺序）。"""
    info = read_case(case_id)
    case_dir = info["case_dir"]
    manifest = info["manifest"]
    results = []
    for item in manifest["sources"]:
        if version is not None and int(item["version"]) != version:
            continue
        path = os.path.join(case_dir, item["path"])
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        parsed = parse_source_text(text, declared_kind=item["kind"],
                                   declared_filename=os.path.basename(item["path"]))
        res = engine.submit_source(project_id, parsed, actor=actor)
        results.append({"kind": item["kind"], "version": item["version"],
                        "event_id": res["event"]["event_id"],
                        "auto_compute": res.get("auto_compute"),
                        "diagnostics": parsed["diagnostics"]})
    return results


def bootstrap_case(engine: Engine, case_id: str, project_id: str,
                   project_name: str = None, actor: str = "alice") -> Dict[str, Any]:
    info = read_case(case_id)
    name = project_name or info["manifest"]["title"]
    if engine.storage.count_events(project_id):
        raise ValueError(f"项目已存在: {project_id}")
    created = engine.create_project(project_id, name, actor=actor)
    submitted = submit_case_sources(engine, case_id, project_id, version=1, actor=actor)
    return {"project_created": created["event_id"], "sources": submitted}


def play_case_step(engine: Engine, case_id: str, project_id: str,
                   step_index: int) -> Dict[str, Any]:
    """执行案例 playbook 的一步（参数修订 / 提交新版本源 / 冲突裁定等）。

    playbook 是机器可读取的裁定与变更脚本；每一步仍以正常事件提交，
    因此与审阅者手工点击产生的历史完全同构、可重放。
    """
    info = read_case(case_id)
    steps = info["manifest"].get("playbook", [])
    if step_index < 0 or step_index >= len(steps):
        raise IndexError("playbook 步骤越界")
    step = steps[step_index]
    kind = step["kind"]
    if kind == "submit_version":
        submitted = submit_case_sources(
            engine, case_id, project_id, version=step["version"],
            actor=step.get("actor", "alice"))
        return {"step": step, "results": submitted}
    if kind == "revise_params":
        event = engine.revise_parameters(project_id, step["params"],
                                         step.get("reason", "playbook"),
                                         actor=step.get("actor", "alice"))
        return {"step": step, "result": event}
    if kind == "decide":
        view = engine.view(project_id)
        candidate = _find_candidate(view, step)
        result = engine.decide(
            project_id, candidate["stable_id"], step["verdict"],
            actor=step.get("actor", "alice"), note=step.get("note", ""),
            new_branch=step.get("new_branch"),
            acknowledge_ambiguity=step.get("acknowledge_ambiguity", False))
        return {"step": step, "result": result}
    if kind == "resolve_conflict":
        conflict_event_id = step["conflict_event_id"]
        if conflict_event_id in ("LATEST_OPEN", "FILL_BY_TEST"):
            open_conflicts = [c for c in engine.view(project_id)["conflicts"]
                              if c["status"] == "open"]
            if not open_conflicts:
                raise EngineError("没有待解决的冲突")
            conflict_event_id = open_conflicts[-1]["event_id"]
        result = engine.resolve_conflict(
            project_id, conflict_event_id, step["resolution"],
            actor=step.get("actor", "alice"),
            winning_decision_id=step.get("winning_decision_id"))
        return {"step": step, "result": result}
    raise ValueError(f"未知 playbook 步骤类型: {kind}")


def _find_candidate(view: Dict[str, Any], step: Dict[str, Any]) -> Dict[str, Any]:
    for c in view["candidates"]:
        if c["peak_key"] == step["peak_key"] and \
                c["atom_label"] == step["atom_label"]:
            return c
    raise KeyError(f"候选未找到: {step.get('peak_key')} -> {step.get('atom_label')}")


def export_bundle(engine: Engine, project_id: str) -> Dict[str, Any]:
    events = engine.storage.all_events(project_id)
    if not events:
        raise KeyError(project_id)
    return {
        "format": BUNDLE_FORMAT,
        "project_id": project_id,
        "name": events[0]["payload"]["name"],
        "exported_at": engine.clock.now(),
        "events": events,
        "heads": engine.storage.list_heads(project_id),
    }


def import_bundle(storage: Storage, bundle: Dict[str, Any], clock=None) -> Engine:
    if bundle.get("format") != BUNDLE_FORMAT:
        raise ValueError(f"不支持的导出包格式: {bundle.get('format')}")
    project_id = bundle["project_id"]
    if storage.count_events(project_id):
        raise ValueError(f"项目已存在: {project_id}")
    engine = Engine(storage, clock=clock)
    for event in sorted(bundle["events"], key=lambda e: (e["seq"], e["event_id"])):
        storage.append_event(event)
        if event["kind"] == "source_submitted":
            record = dict(event["payload"]["source"])
            record["project_id"] = project_id
            record["introduced_event"] = event["event_id"]
            record["diagnostics"] = event["payload"].get("diagnostics", [])
            storage.insert_source(record)
    for head in bundle["heads"]:
        storage.set_head(project_id, head["branch_id"], head["head_event_id"])
    return engine
