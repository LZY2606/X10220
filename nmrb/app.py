"""FastAPI 应用：NMR 共振经纬工作台 HTTP 接口。"""
from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .dependencies import State, get_state
from .scenario import load_case
from .service import ProjectService

app = FastAPI(title="NMR 共振经纬：归属候选网络工作台", version="1.0.0")

STATIC_DIR = pathlib.Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class CreateProject(BaseModel):
    name: str
    project_id: Optional[str] = None


class ImportSourceIn(BaseModel):
    logical_name: str
    text: str
    actor: str = "reviewer"
    change_note: str = ""
    parent_event: Optional[str] = None
    branch: str = "main"


class ParamsIn(BaseModel):
    params: Dict[str, Any]
    actor: str = "spectroscopist"
    note: str = "参数修订"
    parent_event: Optional[str] = None
    branch: str = "main"


class ConditionsIn(BaseModel):
    logical_name: str
    change_kind: str
    text: str
    actor: str = "spectroscopist"
    parent_event: Optional[str] = None
    branch: str = "main"


class DecisionIn(BaseModel):
    site_id: str
    peak_id: Optional[str] = None
    result_id: str
    verdict: str = Field(pattern="^(accept|reject)$")
    actor: str
    rationale: str = ""
    parent_event: str
    branch: Optional[str] = None


class DetectConflictIn(BaseModel):
    site_id: str
    decision_a: str
    decision_b: str
    parent_event: str


class ResolveIn(BaseModel):
    conflict_id: str
    winner_decision: str
    loser_decision: str
    actor: str = "lead"
    rationale: str = ""
    parent_event: str
    branch: str = "main"


class UndoIn(BaseModel):
    decision_event_id: str
    actor: str
    parent_event: str
    branch: str = "main"


class ImportBundleIn(BaseModel):
    bundle: Dict[str, Any]


def svc() -> ProjectService:
    return get_state().service


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/api/projects")
def create_project(body: CreateProject) -> Dict[str, Any]:
    state = get_state()
    pid = body.project_id or f"proj-{uuid.uuid4().hex[:12]}"
    if state.repo.get_project(pid):
        raise HTTPException(409, f"项目已存在：{pid}")
    return state.repo.create_project(pid, body.name)


@app.get("/api/projects/{project_id}/snapshot")
def snapshot(project_id: str, at: Optional[str] = None) -> Dict[str, Any]:
    state = get_state()
    if not state.repo.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    return state.service.snapshot(project_id, at)


@app.post("/api/projects/{project_id}/sources")
def import_source(project_id: str, body: ImportSourceIn) -> Dict[str, Any]:
    return svc().import_source(project_id, body.logical_name, body.text, body.actor,
                               body.change_note or f"导入 {body.logical_name}",
                               body.parent_event, body.branch)


@app.post("/api/projects/{project_id}/params")
def revise_params(project_id: str, body: ParamsIn) -> Dict[str, Any]:
    return svc().revise_params(project_id, body.params, body.actor, body.note,
                               body.parent_event, body.branch)


@app.post("/api/projects/{project_id}/conditions")
def change_conditions(project_id: str, body: ConditionsIn) -> Dict[str, Any]:
    return svc().change_conditions(project_id, body.logical_name, body.change_kind,
                                   body.text, body.actor, body.parent_event, body.branch)


@app.post("/api/projects/{project_id}/decisions")
def decide(project_id: str, body: DecisionIn) -> Dict[str, Any]:
    return svc().decide(project_id, body.site_id, body.peak_id, body.result_id,
                        body.verdict, body.actor, body.rationale, body.parent_event,
                        body.branch)


@app.post("/api/projects/{project_id}/conflicts/detect")
def detect_conflict(project_id: str, body: DetectConflictIn) -> Dict[str, Any]:
    cid = svc().detect_conflict(project_id, body.site_id, body.decision_a,
                                body.decision_b, body.parent_event)
    return {"conflict_id": cid}


@app.post("/api/projects/{project_id}/conflicts/resolve")
def resolve_conflict(project_id: str, body: ResolveIn) -> Dict[str, Any]:
    return svc().resolve(project_id, body.conflict_id, body.winner_decision,
                         body.loser_decision, body.actor, body.rationale,
                         body.parent_event, body.branch)


@app.post("/api/projects/{project_id}/undo")
def undo(project_id: str, body: UndoIn) -> Dict[str, Any]:
    return svc().undo(project_id, body.decision_event_id, body.actor,
                      body.parent_event, body.branch)


@app.get("/api/projects/{project_id}/export")
def export_bundle(project_id: str) -> JSONResponse:
    return JSONResponse(svc().repo.export_bundle(project_id))


@app.post("/api/import-bundle")
def import_bundle(body: ImportBundleIn) -> Dict[str, Any]:
    try:
        pid = svc().repo.import_bundle(body.bundle)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"project_id": pid, "imported": True}


@app.post("/api/demo-case")
def demo_case() -> Dict[str, Any]:
    state = get_state()
    pid = state.ensure_demo()
    return {"project_id": pid, "snapshot": state.service.snapshot(pid)}


@app.get("/api/case-manifest")
def case_manifest() -> Dict[str, Any]:
    manifest = pathlib.Path(__file__).parent / "case" / "MANIFEST.json"
    return JSONResponse(json.loads(manifest.read_text(encoding="utf-8")))


@app.get("/api/case-file")
def case_file(name: str) -> JSONResponse:
    safe = pathlib.Path(name).name
    path = pathlib.Path(__file__).parent / "case" / safe
    if not path.exists():
        raise HTTPException(404, f"案例文件不存在：{safe}")
    return JSONResponse({"name": safe, "text": path.read_text(encoding="utf-8")})


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}
