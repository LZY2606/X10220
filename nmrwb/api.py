"""FastAPI 路由：导入、参数修订、计算、裁定、撤销、冲突解决、重放、导入导出。"""
from __future__ import annotations

import os
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import services
from .casepack import load_casepack
from .core import Store
from .ingest import IngestError
from .page import PAGE


def create_app(store: Store, casepack_dir: Optional[str] = None) -> FastAPI:
    if casepack_dir is None:
        casepack_dir = os.path.join(os.path.dirname(__file__), "..", "casepack")
    app = FastAPI(title="NMR 共振经纬 · 归属候选工作台")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    class InputBody(BaseModel):
        name: str
        format: str
        raw: str

    @app.post("/api/inputs")
    def post_input(body: InputBody):
        try:
            return services.submit_input(store, body.name, body.format, body.raw)
        except IngestError as e:
            raise HTTPException(status_code=400, detail=str(e))

    class ParamsBody(BaseModel):
        updates: Dict

    @app.post("/api/params")
    def post_params(body: ParamsBody):
        return services.revise_params(store, body.updates)

    @app.post("/api/compute")
    def post_compute():
        try:
            return services.compute(store)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/state")
    def get_state(seq: Optional[int] = None):
        return services.current_state(store, upto=seq)

    class DecisionBody(BaseModel):
        atom_id: str
        candidate_id: str
        action: str
        editor: str
        branch: str = "main"

    @app.post("/api/decisions")
    def post_decision(body: DecisionBody):
        try:
            return services.decide(store, body.atom_id, body.candidate_id,
                                   body.action, body.editor, body.branch)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    class UndoBody(BaseModel):
        decision_id: str
        editor: str

    @app.post("/api/undo")
    def post_undo(body: UndoBody):
        return services.undo(store, body.decision_id, body.editor)

    class ResolveBody(BaseModel):
        winner_decision_id: str
        editor: str

    @app.post("/api/conflicts/{cid}/resolve")
    def post_resolve(cid: str, body: ResolveBody):
        return services.resolve_conflict(
            store, cid, body.winner_decision_id, body.editor)

    @app.get("/api/export", response_class=PlainTextResponse)
    def get_export():
        return store.export_jsonl()

    class ImportBody(BaseModel):
        archive: str

    @app.post("/api/import")
    def post_import(body: ImportBody):
        return {"imported": store.import_jsonl(body.archive)}

    @app.post("/api/reset")
    def post_reset():
        store.reset()
        return {"ok": True}

    @app.post("/api/casepack/load")
    def post_casepack():
        return load_casepack(store, casepack_dir)

    return app
