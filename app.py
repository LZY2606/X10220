"""FastAPI 入口：NMR 谱峰归属 / 候选网络工作台。

演示命令：.venv/bin/uvicorn app:app --host 127.0.0.1 --port 5340
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from nmr_core.casework import load_case_pack
from nmr_core.clock import SystemClock, VirtualClock
from nmr_core.ingest import IngestError
from nmr_core.project import ProjectError, ProjectService
from nmr_core.store import EventStore

STATIC_DIR = Path(__file__).parent / "static"


def create_app(db_path: Optional[str] = None, clock=None) -> FastAPI:
    db_path = db_path or os.environ.get("NMR_DB", "nmr_workbench.db")
    store = EventStore(db_path, clock or SystemClock())
    service = ProjectService(store)
    app = FastAPI(title="NMR 谱峰归属工作台")
    app.state.service = service

    def err(handler):
        try:
            return handler()
        except IngestError as e:
            return JSONResponse(status_code=422,
                                content={"error": "ingest_error", "diagnostics": e.diagnostics})
        except ProjectError as e:
            return JSONResponse(status_code=409,
                                content={"error": e.code, "message": str(e)})

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/projects")
    def list_projects():
        out = []
        for pid in store.projects():
            st = service.state(pid)
            out.append({"project_id": pid,
                        "name": st["project"]["name"] if st["project"] else pid,
                        "head_seq": st["head_seq"]})
        return {"projects": out}

    class CreateProject(BaseModel):
        name: str
        narrative: str = ""
        actor: str = "reviewer"

    @app.post("/api/projects")
    def create_project(body: CreateProject):
        return err(lambda: {"project": service.create_project(
            body.name, body.narrative, actor=body.actor)})

    @app.post("/api/case/load")
    def load_case():
        return err(lambda: load_case_pack(service))

    @app.get("/api/projects/{project}/state")
    def get_state(project: str, as_of: Optional[int] = None):
        return err(lambda: service.state(project, as_of=as_of))

    @app.get("/api/projects/{project}/events")
    def get_events(project: str):
        def run():
            return {"events": [
                {"seq": e["seq"], "event_id": e["event_id"], "kind": e["kind"],
                 "actor": e["actor"], "branch": e["branch"],
                 "created_at": e["created_at"]}
                for e in service.events(project)]}
        return err(run)

    class CommitSource(BaseModel):
        source_key: str
        raw_text: str
        actor: str = "reviewer"
        note: str = ""

    @app.post("/api/projects/{project}/sources")
    def commit_source(project: str, body: CommitSource):
        return err(lambda: service.commit_source(
            project, body.source_key, body.raw_text, actor=body.actor, note=body.note))

    class CommitParams(BaseModel):
        params: dict[str, Any]
        actor: str = "reviewer"
        note: str = ""

    @app.post("/api/projects/{project}/params")
    def commit_params(project: str, body: CommitParams):
        return err(lambda: service.commit_params(
            project, body.params, actor=body.actor, note=body.note))

    @app.post("/api/projects/{project}/compute")
    def compute(project: str, actor: str = "reviewer"):
        return err(lambda: service.compute(project, actor=actor))

    class Decide(BaseModel):
        actor: str
        candidate_id: str
        set_id: str
        verdict: str
        rationale: str = ""
        judgment_basis: list[str] = []
        base_event_id: Optional[str] = None

    @app.post("/api/projects/{project}/decisions")
    def decide(project: str, body: Decide):
        return err(lambda: service.decide(
            project, body.actor, body.candidate_id, body.set_id, body.verdict,
            rationale=body.rationale, judgment_basis=body.judgment_basis,
            base_event_id=body.base_event_id))

    class Revert(BaseModel):
        decision_id: str
        actor: str = "reviewer"
        reason: str = ""

    @app.post("/api/projects/{project}/revert")
    def revert(project: str, body: Revert):
        return err(lambda: service.revert_decision(
            project, body.decision_id, actor=body.actor, reason=body.reason))

    class Resolve(BaseModel):
        chosen_decision_id: str
        actor: str = "reviewer"
        rationale: str = ""

    @app.post("/api/projects/{project}/conflicts/{conflict_id}/resolve")
    def resolve(project: str, conflict_id: str, body: Resolve):
        return err(lambda: service.resolve_conflict(
            project, conflict_id, body.chosen_decision_id,
            actor=body.actor, rationale=body.rationale))

    @app.get("/api/projects/{project}/export")
    def export(project: str):
        return err(lambda: service.export(project))

    @app.post("/api/import")
    def import_archive(archive: dict[str, Any]):
        return err(lambda: {"project": service.import_archive(archive)})

    return app


app = create_app()
