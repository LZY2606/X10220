"""NMR 共振经纬工作台 — FastAPI 入口。

演示命令：`.venv/bin/uvicorn app:app --host 127.0.0.1 --port 5340`
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from nmrworkbench.domain import STREAMS
from nmrworkbench.seedcase import seed
from nmrworkbench.service import Workbench
from nmrworkbench.store import EventStore
from nmrworkbench.units import normalize_frequency, normalize_shift, normalize_temperature

STATIC_DIR = Path(__file__).parent / "nmrworkbench" / "static"
DEFAULT_DB = os.environ.get("NMRWB_DB", str(Path(__file__).parent / "nmrwb.sqlite3"))


def build_view(state: dict) -> dict:
    """派生展示层：规范化在这里发生，原始表示不被动摇。"""
    inputs = state["inputs"]
    conditions = inputs.get("conditions", {}).get("data", {})
    field = normalize_frequency(conditions["field"]) if conditions.get("field") else {"canonical": None, "diagnostics": []}
    temp = normalize_temperature(conditions["temperature"]) if conditions.get("temperature") else {"canonical": None, "diagnostics": []}
    field_mhz = field["canonical"]

    def peaks_view(stream: str) -> list:
        out = []
        for p in inputs.get(stream, {}).get("data", {}).get("peaks", []):
            norm = normalize_shift(p["shift"], field_mhz)
            out.append({**p, "canonical_shift_ppm": norm["canonical"], "shift_diagnostics": norm["diagnostics"]})
        return out

    edges = []
    for stream in ("edges_cosy", "edges_hsqc", "edges_hmbc"):
        edges += inputs.get(stream, {}).get("data", {}).get("edges", [])
    latest_set = state["candidate_sets"][-1] if state["candidate_sets"] else None
    return {
        "project": state["project"],
        "peaks_1h": peaks_view("peaks_1h"),
        "peaks_13c": peaks_view("peaks_13c"),
        "edges": edges,
        "skeleton": inputs.get("skeleton", {}).get("data"),
        "conditions": {"raw": conditions, "field_mhz": field_mhz, "temperature_K": temp["canonical"],
                       "diagnostics": field["diagnostics"] + temp["diagnostics"]},
        "candidates": latest_set["candidates"] if latest_set else [],
        "candidate_set_meta": ({k: latest_set[k] for k in ("set_id", "event_id", "algo_version", "param_fingerprint", "sources")}
                               if latest_set else None),
        "candidate_sets_history": [{k: s[k] for k in ("set_id", "event_id", "algo_version", "param_fingerprint", "sources", "branch")}
                                    for s in state["candidate_sets"]],
        "diagnostics": (latest_set["diagnostics"] if latest_set else []) + state["diagnostics"],
        "decisions": list(state["decisions"].values()),
        "conflicts": list(state.get("conflicts", {}).values()),
        "judgments": state["judgments"],
        "assignments": state["assignments"],
        "input_versions": {s: {"version_id": v["version_id"], "parent_version_id": v["parent_version_id"],
                               "source_file": v["source_file"], "committed_event": v["committed_event"]}
                           for s, v in inputs.items()},
        "input_history": state["input_history"],
        "params": state["params"],
        "param_history": state["param_history"],
        "branches": state["branches"],
        "current_branch": state["current_branch"],
        "head_event": state["head_event"],
        "events_seen": state["events_seen"],
    }


class InputCommit(BaseModel):
    stream: str
    data: dict
    source_file: Optional[dict] = None
    actor: str = "reviewer"


class InputRevise(BaseModel):
    data: dict
    note: str = ""
    actor: str = "reviewer"


class ParamsRevise(BaseModel):
    patch: dict
    changed_keys: list[str]
    note: str = ""
    actor: str = "reviewer"


class DecisionIn(BaseModel):
    candidate_id: str
    verdict: str
    editor: str = "reviewer"
    rationale: str = ""
    from_event: Optional[str] = None


class RevertIn(BaseModel):
    actor: str = "reviewer"
    rationale: str = ""


class ResolveIn(BaseModel):
    chosen_decision_id: str
    actor: str = "reviewer"
    rationale: str = ""


def create_app(db_path: str = DEFAULT_DB, clock=None, seed_on_start: bool = True) -> FastAPI:
    store = EventStore(db_path, clock=clock)
    wb = Workbench(store)
    if seed_on_start and store.is_empty():
        seed(wb)
    app = FastAPI(title="NMR 共振经纬工作台")
    app.state.store = store
    app.state.wb = wb
    app.state.db_path = db_path

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def get_state(event_id: Optional[str] = None):
        try:
            return build_view(wb.state(event_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/events")
    def get_events():
        return {"events": store.events(), "branches": store.branches(),
                "current_branch": store.current_branch()}

    @app.post("/api/inputs")
    def commit_input(body: InputCommit):
        try:
            return wb.commit_input(body.stream, body.data, body.source_file or {}, body.actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/inputs/{stream}/revisions")
    def revise_input(stream: str, body: InputRevise):
        try:
            return wb.revise_input(stream, body.data, None, body.actor, body.note)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/params/revisions")
    def revise_params(body: ParamsRevise):
        try:
            return wb.revise_params(body.patch, body.changed_keys, body.actor, body.note)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/regenerate")
    def regenerate(actor: str = "reviewer"):
        try:
            return wb.regenerate(actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/decisions")
    def decide(body: DecisionIn):
        return wb.decide(body.candidate_id, body.verdict, body.editor, body.rationale, body.from_event)

    @app.post("/api/decisions/{decision_id}/revert")
    def revert(decision_id: str, body: RevertIn):
        return wb.revert_decision(decision_id, body.actor, body.rationale)

    @app.post("/api/conflicts/{conflict_id}/resolve")
    def resolve(conflict_id: str, body: ResolveIn):
        return wb.resolve_conflict(conflict_id, body.chosen_decision_id, body.actor, body.rationale)

    @app.get("/api/export")
    def export():
        return store.export()

    @app.post("/api/import")
    def import_archive(archive: dict):
        """删除本地数据库内容并按档案重建：身份、顺序与来源关系保持不变。"""
        store.close()
        if os.path.exists(app.state.db_path):
            os.remove(app.state.db_path)
        fresh = EventStore(app.state.db_path, clock=store.clock)
        try:
            fresh.import_(archive)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        app.state.store = fresh
        app.state.wb = Workbench(fresh)
        # 让模块级引用也指向新库
        import app as self_mod
        self_mod.store = fresh
        self_mod.wb = app.state.wb
        return {"imported_events": len(fresh.events()), "branches": len(fresh.branches())}

    @app.post("/api/seed")
    def reseed():
        if not app.state.store.is_empty():
            raise HTTPException(409, "数据库非空，拒绝重复播种")
        return seed(app.state.wb)

    return app


_app_holder = create_app()
app = _app_holder
store = app.state.store
wb = app.state.wb
